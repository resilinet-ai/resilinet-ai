"""Health, readiness and spans against a real database (NFR-OPS-1)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rai.worker.service import Worker
from tests.integration.apikit import needs_db
from tests.integration.runkit import Lab, config, drain

pytestmark = needs_db


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    seen: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    handler, root = Keep(), logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield seen
    root.removeHandler(handler)
    root.setLevel(level)


def test_the_api_is_ready_with_its_database(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ready", "database": "ok"}
    assert client.get("/healthz").json() == {"status": "ok"}


def test_a_run_is_a_worker_span_with_its_ids(
    lab: Lab, feed: Path, worker_engine: Engine, tmp_path: Path,
    records: list[logging.LogRecord],
) -> None:  # fmt: skip
    run_id = lab.start(lab.plan(lab.tests["batch_0"]))["run_id"]
    drain(Worker(worker_engine, config(feed, tmp_path)))
    assert lab.run(run_id)["state"] == "passed"
    worker_spans = [
        r.fields for r in records
        if getattr(r, "fields", {}).get("span") == "worker.run"
        and r.fields.get("run_id") == run_id
    ]  # fmt: skip
    assert len(worker_spans) == 1
    [done] = worker_spans
    assert (done["outcome"], done["status"], done["attempt"]) == ("finished", "ok", 1)
    assert done["tenant_id"] == str(lab.tenant.tenant_id)
    started = next(r for r in records if r.getMessage().startswith(f"run {run_id}: attempt"))
    assert started.fields["run_id"] == run_id
