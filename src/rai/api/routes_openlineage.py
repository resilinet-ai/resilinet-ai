"""OpenLineage ingest (FR-04.2): an external orchestrator's jobs in a project's lineage.

An orchestrator (Airflow, Dagster, dbt, Spark, ...) with OpenLineage's HTTP transport
posts its run events to ``POST /v1/projects/{id}/openlineage``, with a project API key
that has the ``lineage:write`` scope (or as an engineer). RAI keeps the core of each
event: the type, time, run, job, and input and output datasets. Facets are dropped and
only their names kept: they can carry SQL, paths or environment details, and RAI
needs only the graph. The same event posted twice is stored once.

Engineers map an OpenLineage dataset (namespace and name) to a registered asset
(``POST /v1/assets/{id}/openlineage``). The project's graph,
``GET /v1/projects/{id}/lineage``, shows the assets, their declared inputs (FR-04.1)
and every external job with the datasets it read and wrote, linked to assets where
mapped.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from rai.api.context import ROLES, TenantSession, require
from rai.api.listing import Page, PageOut, after_uuid, page, paged
from rai.api.problems import Problem
from rai.api.routes_projects import find_asset, find_project, is_unique_violation

router = APIRouter(prefix="/v1", tags=["openlineage"])

anyone = require(*ROLES)
engineer = require("engineer")
ingest = require("engineer", key_scope="lineage:write")

MAX_DATASETS = 200  # inputs or outputs per event
MAX_FACETS = 100
EventType = Literal["START", "RUNNING", "COMPLETE", "FAIL", "ABORT", "OTHER"]
Text = Annotated[str, Field(min_length=1, max_length=500)]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")  # OpenLineage adds fields; RAI keeps its core


class DatasetIn(_Open):
    namespace: Text
    name: Text
    facets: dict[str, Any] | None = None
    inputFacets: dict[str, Any] | None = None
    outputFacets: dict[str, Any] | None = None


class RunIn(_Open):
    runId: UUID
    facets: dict[str, Any] | None = None


class JobIn(_Open):
    namespace: Text
    name: Text
    facets: dict[str, Any] | None = None


class RunEventIn(_Open):
    """An OpenLineage RunEvent (spec 2-0-x); its other fields are accepted and dropped."""

    eventType: EventType = "OTHER"
    eventTime: datetime
    run: RunIn
    job: JobIn
    inputs: list[DatasetIn] = Field(default_factory=list, max_length=MAX_DATASETS)
    outputs: list[DatasetIn] = Field(default_factory=list, max_length=MAX_DATASETS)
    producer: Text
    schemaURL: str | None = Field(default=None, max_length=500)


class IngestOut(BaseModel):
    event_id: UUID
    stored: bool  # false: the same event was already stored


class DatasetRef(BaseModel):
    namespace: str
    name: str
    asset_id: UUID | None = None  # the mapped asset, if any


class EventOut(BaseModel):
    event_id: UUID
    event_type: EventType
    event_time: datetime
    run_id: UUID
    job_namespace: str
    job_name: str
    inputs: list[DatasetRef]
    outputs: list[DatasetRef]
    facet_names: list[str]
    producer: str
    received_at: datetime


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: Text
    name: Text


class MappingOut(BaseModel):
    mapping_id: UUID
    asset_id: UUID
    namespace: str
    name: str
    created_by: UUID
    created_at: datetime


class GraphAsset(BaseModel):
    asset_id: UUID
    name: str
    kind: str
    openlineage: MappingIn | None


class GraphInput(BaseModel):
    asset_id: UUID
    input_asset_id: UUID
    transform_ref: str | None


class LastEvent(BaseModel):
    event_type: EventType
    event_time: datetime
    run_id: UUID


class GraphJob(BaseModel):
    namespace: str
    name: str
    runs: int
    last_event: LastEvent
    inputs: list[DatasetRef]
    outputs: list[DatasetRef]


class GraphOut(BaseModel):
    project_id: UUID
    assets: list[GraphAsset]
    inputs: list[GraphInput]
    jobs: list[GraphJob]


def _facet_names(event: RunEventIn) -> list[str]:
    names: set[str] = set()
    for scope, facets in (("run", event.run.facets), ("job", event.job.facets)):
        names.update(f"{scope}.{name}" for name in facets or {})
    for dataset in (*event.inputs, *event.outputs):
        for kind in ("facets", "inputFacets", "outputFacets"):
            names.update(f"dataset.{name}" for name in getattr(dataset, kind) or {})
    return sorted(names)[:MAX_FACETS]


def _datasets(datasets: list[DatasetIn]) -> list[dict[str, str]]:
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for d in datasets:
        seen.setdefault((d.namespace, d.name), {"namespace": d.namespace, "name": d.name})
    return list(seen.values())


def _mapped(session: TenantSession, project_id: UUID) -> dict[tuple[str, str], UUID]:
    rows = session.conn.execute(
        text("SELECT namespace, name, asset_id FROM rai.openlineage_datasets "
             "WHERE project_id = :p"), {"p": project_id},
    ).all()  # fmt: skip
    return {(r.namespace, r.name): r.asset_id for r in rows}


def _refs(datasets: list[dict[str, str]], mapped: dict[tuple[str, str], UUID]) -> list[DatasetRef]:
    return [
        DatasetRef(namespace=d["namespace"], name=d["name"],
                   asset_id=mapped.get((d["namespace"], d["name"])))
        for d in datasets
    ]  # fmt: skip


def _project_for(session: TenantSession, project_id: UUID, *, for_change: bool = False) -> None:
    if not session.own_project(project_id):  # a key sees only its own project
        raise Problem(404, "projects.not_found", "Project not found")
    find_project(session.conn, project_id, for_change=for_change)


@router.post("/projects/{project_id}/openlineage", status_code=201)
def ingest_event(
    project_id: UUID,
    event: RunEventIn,
    response: Response,
    session: Annotated[TenantSession, Depends(ingest)],
) -> IngestOut:
    """Store an OpenLineage RunEvent's core. 201 when new, 200 when already stored."""
    _project_for(session, project_id, for_change=True)
    canonical = json.dumps(event.model_dump(mode="json", exclude_none=True), sort_keys=True,
                           separators=(",", ":"))  # fmt: skip
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    existing = session.conn.execute(
        text("SELECT event_id FROM rai.openlineage_events WHERE project_id = :p "
             "AND event_sha256 = :h"), {"p": project_id, "h": digest},
    ).scalar_one_or_none()  # fmt: skip
    if existing is not None:
        response.status_code = 200
        return IngestOut(event_id=existing, stored=False)
    event_id = session.conn.execute(
        text(
            "INSERT INTO rai.openlineage_events (project_id, event_sha256, event_type, "
            "event_time, run_id, job_namespace, job_name, inputs, outputs, facet_names, "
            "producer) VALUES (:p, :h, :t, :at, :r, :jn, :j, CAST(:i AS jsonb), "
            "CAST(:o AS jsonb), :f, :producer) RETURNING event_id"
        ),
        {"p": project_id, "h": digest, "t": event.eventType, "at": event.eventTime,
         "r": event.run.runId, "jn": event.job.namespace, "j": event.job.name,
         "i": json.dumps(_datasets(event.inputs)), "o": json.dumps(_datasets(event.outputs)),
         "f": _facet_names(event), "producer": event.producer},
    ).scalar_one()  # fmt: skip
    response.headers["Location"] = f"/v1/projects/{project_id}/openlineage/events/{event_id}"
    return IngestOut(event_id=event_id, stored=True)


@router.get("/projects/{project_id}/openlineage/events")
def list_events(
    project_id: UUID,
    session: Annotated[TenantSession, Depends(anyone)],
    wanted: Annotated[Page, Depends(page)],
    job_namespace: Annotated[str | None, Query(max_length=500)] = None,
    job_name: Annotated[str | None, Query(max_length=500)] = None,
) -> PageOut[EventOut]:
    """Stored events, oldest received first; optionally one job's."""
    _project_for(session, project_id)
    mapped = _mapped(session, project_id)
    rows = session.conn.execute(
        text(
            "SELECT event_id, event_type, event_time, run_id, job_namespace, job_name, inputs, "
            "outputs, facet_names, producer, received_at FROM rai.openlineage_events "
            "WHERE project_id = :p AND (CAST(:ns AS text) IS NULL OR job_namespace = :ns) "
            "AND (CAST(:jn AS text) IS NULL OR job_name = :jn) "
            "AND (CAST(:after AS uuid) IS NULL OR event_id > :after) "
            "ORDER BY event_id LIMIT :n"
        ),
        {"p": project_id, "ns": job_namespace, "jn": job_name, "after": after_uuid(wanted),
         "n": wanted.limit + 1},
    ).all()  # fmt: skip
    events = [
        EventOut(**(r._asdict() | {"inputs": _refs(r.inputs, mapped),
                                   "outputs": _refs(r.outputs, mapped),
                                   "facet_names": list(r.facet_names)}))
        for r in rows
    ]  # fmt: skip
    return paged(events, wanted, lambda e: str(e.event_id))


@router.post("/assets/{asset_id}/openlineage", status_code=201)
def map_dataset(
    asset_id: UUID,
    body: MappingIn,
    response: Response,
    session: Annotated[TenantSession, Depends(engineer)],
) -> MappingOut:
    """This asset is the OpenLineage dataset ``namespace``/``name``."""
    asset = find_asset(session.conn, asset_id)
    find_project(session.conn, asset.project_id, for_change=True)
    try:
        with session.conn.begin_nested():
            session.conn.execute(
                text("INSERT INTO rai.openlineage_datasets (project_id, asset_id, namespace, "
                     "name, created_by) VALUES (:p, :a, :ns, :n, :u)"),
                {"p": asset.project_id, "a": asset_id, "ns": body.namespace, "n": body.name,
                 "u": session.user_id},
            )  # fmt: skip
    except IntegrityError as error:
        if is_unique_violation(error):
            raise Problem(
                409, "openlineage.already_mapped",
                "This asset, or this dataset, is already mapped in the project",
                detail="remove the mapping first: one dataset per asset, one asset per dataset",
            ) from None  # fmt: skip
        raise
    response.headers["Location"] = f"/v1/assets/{asset_id}/openlineage"
    return _mapping(session, asset_id)


def _mapping(session: TenantSession, asset_id: UUID) -> MappingOut:
    row = session.conn.execute(
        text("SELECT mapping_id, asset_id, namespace, name, created_by, created_at "
             "FROM rai.openlineage_datasets WHERE asset_id = :a"), {"a": asset_id},
    ).first()  # fmt: skip
    if row is None:
        raise Problem(404, "openlineage.not_mapped", "This asset is not mapped to a dataset")
    return MappingOut.model_validate(row._asdict())


@router.get("/assets/{asset_id}/openlineage")
def get_mapping(asset_id: UUID, session: Annotated[TenantSession, Depends(anyone)]) -> MappingOut:
    find_asset(session.conn, asset_id)
    return _mapping(session, asset_id)


@router.delete("/assets/{asset_id}/openlineage", status_code=204)
def remove_mapping(
    asset_id: UUID, session: Annotated[TenantSession, Depends(engineer)]
) -> Response:
    """Remove a wrong mapping (audited). Stored events are unchanged."""
    asset = find_asset(session.conn, asset_id)
    find_project(session.conn, asset.project_id, for_change=True)
    removed = session.conn.execute(
        text("DELETE FROM rai.openlineage_datasets WHERE asset_id = :a"), {"a": asset_id}
    ).rowcount
    if not removed:
        raise Problem(404, "openlineage.not_mapped", "This asset is not mapped to a dataset")
    return Response(status_code=204)


@router.get("/projects/{project_id}/lineage")
def project_lineage(
    project_id: UUID, session: Annotated[TenantSession, Depends(anyone)]
) -> GraphOut:
    """The project's graph: assets, declared inputs, and every external job with the
    datasets it read and wrote (linked to assets where mapped)."""
    _project_for(session, project_id)
    conn = session.conn
    mapped = _mapped(session, project_id)
    by_asset = {asset: key for key, asset in mapped.items()}
    assets = [
        GraphAsset(asset_id=r.asset_id, name=r.name, kind=r.kind,
                   openlineage=MappingIn(namespace=by_asset[r.asset_id][0],
                                         name=by_asset[r.asset_id][1])
                   if r.asset_id in by_asset else None)
        for r in conn.execute(
            text("SELECT asset_id, name, kind::text FROM rai.pipeline_assets "
                 "WHERE project_id = :p ORDER BY asset_id"), {"p": project_id})
    ]  # fmt: skip
    inputs = [
        GraphInput.model_validate(r._asdict())
        for r in conn.execute(
            text("SELECT asset_id, input_asset_id, transform_ref FROM rai.asset_inputs "
                 "WHERE project_id = :p ORDER BY input_id"), {"p": project_id})
    ]  # fmt: skip
    jobs = []
    for r in conn.execute(
        text(
            "SELECT job_namespace, job_name, count(DISTINCT run_id) AS runs, "
            "jsonb_agg(DISTINCT d) FILTER (WHERE d IS NOT NULL) AS inputs, "
            "(SELECT jsonb_agg(DISTINCT o) FROM rai.openlineage_events e2, "
            "  jsonb_array_elements(e2.outputs) o WHERE e2.project_id = :p "
            "  AND e2.job_namespace = e.job_namespace AND e2.job_name = e.job_name) AS outputs, "
            "(SELECT jsonb_build_object('event_type', l.event_type, 'event_time', l.event_time, "
            "  'run_id', l.run_id) FROM rai.openlineage_events l WHERE l.project_id = :p "
            "  AND l.job_namespace = e.job_namespace AND l.job_name = e.job_name "
            "  ORDER BY l.event_time DESC, l.event_id DESC LIMIT 1) AS last_event "
            "FROM rai.openlineage_events e LEFT JOIN LATERAL jsonb_array_elements(e.inputs) d "
            "ON true WHERE e.project_id = :p GROUP BY job_namespace, job_name "
            "ORDER BY job_namespace, job_name"
        ),
        {"p": project_id},
    ):
        jobs.append(GraphJob(
            namespace=r.job_namespace, name=r.job_name, runs=r.runs,
            last_event=LastEvent.model_validate(r.last_event),
            inputs=_refs(sorted(r.inputs or [], key=_order), mapped),
            outputs=_refs(sorted(r.outputs or [], key=_order), mapped),
        ))  # fmt: skip
    return GraphOut(project_id=project_id, assets=assets, inputs=inputs, jobs=jobs)


def _order(dataset: dict[str, str]) -> tuple[str, str]:
    return dataset["namespace"], dataset["name"]
