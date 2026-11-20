"""The evidence bundle's human-readable report and limitations (B-45: FR-13.1, NFR-AUD-2).

``report.md`` sits in every evidence bundle beside the JSON members it is rendered
from: what ran, against which pinned versions and images, what each step concluded,
recovery against its objectives, and a **limitations statement**: what this evidence
does *not* show. The same limitations are listed in ``bundle.json``.

The report adds nothing the JSON members do not hold, and it is deterministic like
them: no export time and fixed ordering, so two exports of a run are still
byte-identical. Where the report and a JSON member differ, the JSON member is the
record.

Limitations are stated, not inferred: each one is either always true of RAI evidence
(it is tamper-evident, not proof that the recorded facts were true) or follows from
something in the bundle (an unfinished run, steps in error, container images that
were not pinned, an unsigned bundle).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from rai.core.evidence import STATEMENT, plain

COMPLETE = ("passed", "failed")
LIMIT_SCOPE = (
    "The results hold for the pinned inputs listed in this report, at the time of the run. "
    "A later change to the data, the contracts or the system under test is not covered."
)
LIMIT_EXCLUDED = (
    "The bundle holds no data and no secrets: synthetic data appears only as SHA-256 "
    "digests (lineage.json), and pins hold secret references, never values."
)
LIMIT_RECOVERY = (
    "Recovery times come from RAI's own probes, about one round a second, measured from the "
    "fault's onset as the chaos agent recorded it. Their accuracy is checked separately, "
    "against an independent observer (Protocol H3); that check is not part of this bundle."
)
LIMIT_UNSIGNED = (
    "The bundle is not signed: its manifest shows whether a member changed after export, "
    "but not who exported it."
)


def _cell(value: Any) -> str:
    """A Markdown table cell: never breaks the table, whatever the value holds."""
    if value is None or value == "":
        return "-"
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.split())


def _table(header: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def _item_label(item: Mapping[str, Any]) -> tuple[str, str]:
    for kind in ("test", "scenario"):
        what = item.get(kind)
        if isinstance(what, Mapping):
            return kind, f"{what.get('name')} v{what.get('version')}"
    return "-", "-"


def _scenario_targets(items: list[Mapping[str, Any]]) -> set[str]:
    """Container-capable targets the run's scenarios act on."""
    names: set[str] = set()
    for item in items:
        scenario = item.get("scenario")
        if isinstance(scenario, Mapping):
            for fault in scenario.get("faults") or ():
                if isinstance(fault, Mapping) and fault.get("target"):
                    names.add(str(fault["target"]))
    return names


def limitations(members: Mapping[str, Any], signed: bool) -> list[str]:
    """What this evidence does not show, in a fixed order."""
    run = plain(members["run.json"])
    steps, recovery = plain(members["steps.json"]), plain(members["recovery.json"])
    pins = run.get("pins") or {}
    environment = pins.get("environment") if isinstance(pins, Mapping) else None
    items = [i for i in (pins.get("items") or []) if isinstance(i, Mapping)]
    out: list[str] = []
    if isinstance(environment, Mapping):
        out.append(
            f"The run executed in environment {environment.get('name')} "
            f"({environment.get('type')}). RAI never runs against production, so these results "
            "describe that environment, not a production system."
        )
    else:
        out.append(
            "The run had no environment: it checked the data assets it read, and says nothing "
            "about the systems that produce them."
        )
    out.append(LIMIT_SCOPE)
    if run.get("state") not in COMPLETE:
        out.append(
            f"The run did not complete (state {run.get('state')}): steps it did not reach were "
            "not executed, and a missing failure is not a pass."
        )
    errored = [s["step_key"] for s in steps if s.get("state") == "error"]
    if errored:
        out.append(
            f"{len(errored)} step(s) ended in error and gave no verdict: {', '.join(errored)}."
        )
    targets = _scenario_targets(items)
    if targets and isinstance(environment, Mapping):
        if "images" not in environment:
            out.append(
                "This run predates image pinning (B-45): the container images it acted on are "
                "not recorded, so a repeat may act on different software."
            )
        else:
            unpinned = sorted(targets & set(environment.get("unpinned_images") or ()))
            if unpinned:
                out.append(
                    f"Container images were not pinned for {', '.join(unpinned)} (registered "
                    "without an image digest): a repeat may act on different software."
                )
    if recovery:
        out.append(LIMIT_RECOVERY)
    if not signed:
        out.append(LIMIT_UNSIGNED)
    out.append(LIMIT_EXCLUDED)
    out.append(STATEMENT)
    return out


def render(members: Mapping[str, Any], bundle: Mapping[str, Any]) -> str:
    """``report.md``: Markdown, LF line endings, one trailing newline."""
    run, steps = plain(members["run.json"]), plain(members["steps.json"])
    observations, recovery = plain(members["observations.json"]), plain(members["recovery.json"])
    info = plain(bundle)
    pins = run.get("pins") or {}
    plan = pins.get("plan") or {}
    environment = pins.get("environment")
    items = [i for i in (pins.get("items") or []) if isinstance(i, Mapping)]
    trigger = (f"API key {run['api_key_id']}" if run.get("api_key_id")
               else f"user {run.get('triggered_by')}")  # fmt: skip
    lines = [
        f"# RAI evidence report: run {run['run_id']}",
        "",
        "Rendered from the JSON members of this bundle, and holding nothing they do not. "
        "Where this report and a JSON member differ, the JSON member is the record.",
        "",
        "## Summary",
        "",
        *_table(
            ("Field", "Value"),
            [
                ("Run", run["run_id"]),
                ("Project", run.get("project_id")),
                ("State", run.get("state")),
                ("Verdict", run.get("verdict")),
                ("Reason", run.get("reason")),
                ("Plan", f"{plan.get('name')} v{plan.get('version')}" if plan else None),
                (
                    "Environment",
                    (
                        f"{environment.get('name')} ({environment.get('type')})"
                        if isinstance(environment, Mapping)
                        else None
                    ),
                ),
                ("RAI version", run.get("rai_version")),
                ("Triggered by", trigger),
                ("Re-run of", run.get("rerun_of")),
                ("Queued", run.get("queued_at")),
                ("Started", run.get("started_at")),
                ("Finished", run.get("finished_at")),
            ],
        ),
        "",
        "## Steps",
        "",
    ]
    by_key = {i.get("step_key"): i for i in items}
    lines += _table(
        ("Step", "Kind", "Runs", "State", "Verdict", "Error"),
        [(s["step_key"], *_item_label(by_key.get(s["step_key"], {})), s.get("state"),
          s.get("verdict"), s.get("error")) for s in steps],
    )  # fmt: skip
    outcomes = Counter(str(o.get("outcome")) for o in observations)
    lines += [
        "",
        "Observations: "
        + (", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) or "none")
        + " (observations.json).",
    ]
    if recovery:
        lines += ["", "## Recovery", ""]
        lines += _table(
            ("Step", "RTO s", "RTO objective s", "RTO met", "RPO s", "RPO objective s",
             "RPO met", "Lost rows"),
            [(r["step_key"], r.get("rto_seconds"), r.get("rto_objective_seconds"),
              r.get("rto_met"), r.get("rpo_seconds"), r.get("rpo_objective_seconds"),
              r.get("rpo_met"), r.get("lost_rows")) for r in recovery],
        )  # fmt: skip
        lines += ["", "An empty RTO means not restored within the window: a miss, not a gap."]
    lines += ["", "## Pinned inputs", ""]
    pinned_rows: list[tuple[Any, ...]] = []
    for item in items:
        kind, label = _item_label(item)
        contract = item.get("contract") or {}
        asset = item.get("asset") or {}
        scenario = item.get("scenario") or {}
        pinned_rows.append((
            item.get("step_key"), kind, label, asset.get("name"),
            f"{contract.get('ref')} sha256:{contract.get('spec_sha256')}" if contract else None,
            scenario.get("approval_id") if isinstance(scenario, Mapping) else None,
        ))  # fmt: skip
    lines += _table(("Step", "Kind", "Version", "Asset", "Contract", "Approval"), pinned_rows)
    if isinstance(environment, Mapping) and (
        environment.get("images") or environment.get("unpinned_images")
    ):
        lines += ["", "Container images, pinned when the run was queued:", ""]
        images = environment.get("images") or {}
        image_rows = [(name, digest) for name, digest in sorted(images.items())]
        image_rows += [(name, "not pinned") for name in environment.get("unpinned_images") or ()]
        lines += _table(("Target", "Image"), image_rows)
    lines += ["", "## Limitations", ""]
    lines += [f"- {text}" for text in info.get("limitations") or ()]
    anchor = info.get("anchor") or {}
    signature = info.get("signature")
    lines += [
        "",
        "## Integrity",
        "",
        f"- Audit-chain anchor: entry {anchor.get('seq')}, record hash "
        f"`{anchor.get('record_hash')}` ({anchor.get('action')}).",
        "- manifest.json lists the SHA-256 of every member, this report included.",
        (f"- Signed with Ed25519 key {signature.get('key_id')} (signature.json). Check it with "
         "`rai verify --key`, using a public key you got from somewhere other than this bundle."
         if isinstance(signature, Mapping) else "- Not signed."),
    ]  # fmt: skip
    return "\n".join(lines) + "\n"
