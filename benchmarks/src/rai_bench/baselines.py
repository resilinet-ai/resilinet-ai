"""Baseline arms B1 and B2 (Protocol section 3; B-21), written for external review.

Both are written only from the pipeline specification (SandboxBank schema v1) and
30 clean development batches, never from defect fixtures (fairness rules). They
see exactly what arm R sees: each delivered batch, the reference tables in
``feed/``, the merchants dimension delivered with a batch, and its arrival time.

* **B1, conventional suite**: the pytest file ``benchmarks/baselines/b1/test_feed.py``,
  run once per delivery in its own process, as a CI job would run it. One alert
  per failing test.
* **B2, Great Expectations**: suites auto-profiled on 30 clean development
  batches (``profile_suite``), then hand-tightened from the specification
  (``TIGHTENINGS``; each one quotes the spec it comes from). The frozen suites
  are ``benchmarks/baselines/b2/*.json``. One alert per failing expectation.

Great Expectations 1.x has no built-in profiler (it was removed in 1.0), so
``profile_suite`` reproduces the classic rules openly: every rule is listed in
its docstring for the reviewer.

Defect-class claims come from what each check is, using the public defect
catalogue (Protocol section 4), exactly as arm R's do; never from fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rai_bench.alerts import Alert
from rai_bench.arms import Clock, Delivery

ROOT = Path(__file__).resolve().parents[3]
B1_DIR = ROOT / "benchmarks" / "baselines" / "b1"
B2_DIR = ROOT / "benchmarks" / "baselines" / "b2"
WINDOW = timedelta(seconds=900)  # SandboxBank batch window (README: --window, default 900 s)
LOW_CARDINALITY = 20  # profiler: at most this many distinct values -> a value set


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- B1 --------------------------------------------------------------------------------------


class B1Error(RuntimeError):
    """The B1 suite could not run (not the same as a failing test)."""


def b1_claim(test: str, table: str, detail: str) -> str | None:
    """B1's defect-class claim for one failing test (catalogue section 4)."""
    if test == "test_primary_key":
        return "D12" if table == "merchants" else "D01"
    if test == "test_required":
        return "D03"
    if test == "test_type":
        return "D04"
    if test == "test_columns":
        missing = "missing: []" not in detail
        unexpected = "unexpected: []" not in detail
        return "D07" if missing and unexpected else "D08" if missing else None
    return None


def _columns_named(detail: str) -> tuple[str, ...]:
    """Column names in a test_columns message: ``missing: ['a']; unexpected: ['b']``."""
    names: list[str] = []
    for part in detail.split(";"):
        _, _, listed = part.partition(":")
        names += [n.strip(" '\"[]") for n in listed.split(",") if n.strip(" '\"[]")]
    return tuple(names)


def b1_alerts(junit: Path, batch_id: str, moment: Any) -> list[Alert]:
    """One alert per failing test in a JUnit XML report."""
    alerts: list[Alert] = []
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - our own pytest output
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        name = case.get("name", "")
        test, _, params = name.partition("[")
        table, _, column = params.rstrip("]").partition("-")
        detail = (failure.get("message") or "").splitlines()[0].removeprefix("AssertionError: ")
        if test == "test_columns":
            location = _columns_named(detail)
        elif test == "test_primary_key":  # "txn_id: 2 of 300 rows repeat a key in this file"
            location = (detail.split(":", 1)[0],)
        else:
            location = (column,) if column else ()
        prefix = "merchants dimension: " if table == "merchants" else ""
        alerts.append(
            Alert(
                arm="B1", subject_kind="batch", subject_id=batch_id, time=moment,
                location=location, message=f"{prefix}{test}: {detail}",
                defect_class=b1_claim(test, table, detail),
            )
        )  # fmt: skip
    return alerts


class B1Arm:
    """Arm B1: the conventional pytest suite, one pytest process per delivery."""

    name = "B1"

    def __init__(self, suite: Path = B1_DIR) -> None:
        self._suite = suite
        self._workdir: Path | None = None

    def describe(self) -> dict[str, Any]:
        test_file = self._suite / "test_feed.py"
        return {
            "arm": self.name,
            "tool": "pytest",
            "suite": {
                "test_feed.py": _sha256(test_file),
                "pytest.ini": _sha256(self._suite / "pytest.ini"),
            },
        }

    def open(self, workdir: Path, feed: Path) -> None:
        self._workdir = workdir

    def observe(self, delivery: Delivery, clock: Clock) -> list[Alert]:
        if self._workdir is None:
            raise RuntimeError("open() the arm before observing batches")
        junit = self._workdir / f"junit-{delivery.batch_id}.xml"
        env = os.environ | {"RAI_B1_BATCH": str(delivery.file)}
        env.pop("RAI_B1_DIMENSION", None)
        if delivery.dimension is not None:
            env["RAI_B1_DIMENSION"] = str(delivery.dimension)
        done = subprocess.run(  # noqa: S603 - a fixed command: this interpreter running pytest
            [sys.executable, "-m", "pytest", str(self._suite),
             "-c", str(self._suite / "pytest.ini"), "--rootdir", str(self._suite),
             f"--junitxml={junit}"],
            env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if done.returncode not in (0, 1):  # 0 all passed, 1 some failed
            raise B1Error(f"B1 suite could not run (exit {done.returncode}): {done.stdout[-500:]}")
        return b1_alerts(junit, delivery.batch_id, clock())

    def close(self) -> None:
        self._workdir = None


# ---- B2: profiling and tightening ------------------------------------------------------------


def _gx() -> Any:
    """Import Great Expectations with its analytics off (no data leaves the machine)."""
    os.environ["GX_ANALYTICS_ENABLED"] = "False"
    import great_expectations as gx

    return gx


# Columns schema v1 gives as timestamps: read as such, as a pandas team would.
TIMESTAMPS = {"transactions_raw": ["created_at"]}


def _read(path: Path, table: str = "") -> Any:
    """How a team reading a CSV with pandas would: default type inference, and the
    specified timestamp columns parsed (a malformed value leaves the column as text)."""
    import pandas as pd  # type: ignore[import-untyped]

    dates = [c for c in TIMESTAMPS.get(table, []) if c in pd.read_csv(path, nrows=0).columns]
    return pd.read_csv(path, parse_dates=dates) if dates else pd.read_csv(path)


def profile_suite(frames: Sequence[Any], name: str) -> dict[str, Any]:
    """Expectations from clean batches, with the rules of the classic GX profiler:

    * T1 ``expect_table_columns_to_match_ordered_list``: the observed columns.
    * T2 ``expect_table_row_count_to_be_between``: observed minimum and maximum rows.
    * C1 ``expect_column_values_to_be_in_type_list``: the observed pandas dtypes (for text
      columns and extension dtypes such as tz-aware timestamps, the Python types of the
      values, which is what GX checks there).
    * C2 ``expect_column_values_to_not_be_null``: when no batch had a null.
    * C3 ``expect_column_values_to_be_unique``: when every batch was unique.
    * C4 ``expect_column_values_to_be_in_set``: at most 20 distinct values overall.
    * C5 ``expect_column_values_to_be_between``: observed min and max of numeric columns.
    """
    from pandas.api.types import (  # type: ignore[import-untyped]
        is_extension_array_dtype as is_extension,
    )

    first = frames[0]
    columns = list(first.columns)
    expectations: list[dict[str, Any]] = [
        {"type": "expect_table_columns_to_match_ordered_list", "kwargs": {"column_list": columns}},
        {"type": "expect_table_row_count_to_be_between",
         "kwargs": {"min_value": min(len(f) for f in frames),
                    "max_value": max(len(f) for f in frames)}},
    ]  # fmt: skip
    for column in columns:
        series = [f[column] for f in frames]
        dtypes = sorted({str(s.dtype) for s in series})
        types: set[str] = set()
        for s in series:
            if s.dtype == object or is_extension(s.dtype):  # GX checks the values' types
                types |= {type(v).__name__ for v in s.dropna().unique().tolist()}
            else:  # a plain numpy dtype: GX checks the column's dtype
                types.add(str(s.dtype))
        expectations.append(
            {"type": "expect_column_values_to_be_in_type_list",
             "kwargs": {"column": column, "type_list": sorted(types)}}
        )  # fmt: skip
        if all(s.notna().all() for s in series):
            expectations.append(
                {"type": "expect_column_values_to_not_be_null", "kwargs": {"column": column}}
            )
        if all(s.dropna().is_unique for s in series):
            expectations.append(
                {"type": "expect_column_values_to_be_unique", "kwargs": {"column": column}}
            )
        distinct: set[Any] = set()
        for s in series:
            distinct |= set(s.dropna().unique().tolist())
            if len(distinct) > LOW_CARDINALITY:
                break
        if len(distinct) <= LOW_CARDINALITY and dtypes != ["bool"]:
            expectations.append(
                {"type": "expect_column_values_to_be_in_set",
                 "kwargs": {"column": column, "value_set": sorted(distinct, key=str)}}
            )  # fmt: skip
        if all(d in ("int64", "float64") for d in dtypes):
            expectations.append(
                {"type": "expect_column_values_to_be_between",
                 "kwargs": {"column": column, "min_value": float(min(s.min() for s in series)),
                            "max_value": float(max(s.max() for s in series))}}
            )  # fmt: skip
    for e in expectations:
        e["meta"] = {"source": "profiled"}
    return {"name": name, "expectations": expectations}


# Hand-tightening (Protocol section 3: "then hand-tightened (documented)"). Each entry
# quotes the specification it comes from. "replaces" swaps a profiled expectation the
# specification contradicts (an over-fitted bound would raise false alarms); an entry
# with only "removes" drops a profiled expectation, with its reason.
TIGHTENINGS: dict[str, list[dict[str, Any]]] = {
    "transactions_raw": [
        {"id": "K1", "spec": "account_id: References accounts.account_id",
         "type": "expect_column_values_to_be_in_set",
         "kwargs": {"column": "account_id", "value_set": {"$PARAMETER": "account_ids"}}},
        {"id": "K2",
         "spec": "merchant_id: References merchants.merchant_id; empty for ach, wire and p2p",
         "type": "expect_column_values_to_be_in_set",
         "kwargs": {"column": "merchant_id", "value_set": {"$PARAMETER": "merchant_ids"}}},
        {"id": "K3", "spec": "txn_id (UUID)", "type": "expect_column_values_to_match_regex",
         "kwargs": {"column": "txn_id",
                    "regex": r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}},
        {"id": "K4", "spec": "created_at: UTC, millisecond precision, inside the batch window",
         "type": "expect_column_values_to_be_between",
         "kwargs": {"column": "created_at", "min_value": {"$PARAMETER": "window_start"},
                    "max_value": {"$PARAMETER": "window_end"}}},
        {"id": "K5", "spec": "amount: Positive, 2 dp, at most the account txn_limit",
         "replaces": {"type": "expect_column_values_to_be_between", "column": "amount"},
         "type": "expect_column_values_to_be_between",
         "kwargs": {"column": "amount", "min_value": 0.01,
                    "max_value": {"$PARAMETER": "max_txn_limit"}}},
        {"id": "K6", "spec": "batch_id: B followed by 6 digits (the delivered file's batch)",
         "type": "expect_column_values_to_be_in_set",
         "kwargs": {"column": "batch_id", "value_set": {"$PARAMETER": "batch_ids"}}},
        {"id": "R1", "spec": "created_at (UTC, ms): parsed as a timestamp",
         "removes": {"type": "expect_column_values_to_be_in_type_list", "column": "created_at"},
         "reason": "Great Expectations 1.24 cannot match the type of a time-zone-aware "
                   "timestamp column, so the profiled check fails on every clean batch"},
    ],
    "merchants": [
        {"id": "K7", "spec": "merchant_id: Primary key, MER- followed by 6 digits",
         "type": "expect_column_values_to_match_regex",
         "kwargs": {"column": "merchant_id", "regex": r"^MER-\d{6}$"}},
    ],
}  # fmt: skip


def tighten(profiled: dict[str, Any], table: str) -> dict[str, Any]:
    expectations = list(profiled["expectations"])
    for edit in TIGHTENINGS[table]:
        replaced = edit.get("replaces") or edit.get("removes")
        if replaced:
            expectations = [
                e for e in expectations
                if not (e["type"] == replaced["type"]
                        and e["kwargs"].get("column") == replaced["column"])
            ]  # fmt: skip
        if "type" not in edit:
            continue
        expectations.append(
            {"type": edit["type"], "kwargs": edit["kwargs"],
             "meta": {"source": "tightened", "id": edit["id"], "spec": edit["spec"]}}
        )  # fmt: skip
    meta = profiled.get("meta", {})
    return {"name": profiled["name"], "expectations": expectations, "meta": meta}


def write_suites(dataset: Path, out: Path) -> dict[str, Path]:
    """Profile the clean development dataset (written by ``sandboxbank generate``) and
    write the profiled and the tightened suites, in a reviewable, stable JSON form."""
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("seed_purpose") != "development":
        raise ValueError("B2 is profiled on development data only (Protocol section 3)")
    batches = sorted((dataset / "transactions_raw").glob("B*.csv"))
    frames = {
        "transactions_raw": [_read(p, "transactions_raw") for p in batches],
        "merchants": [_read(dataset / "merchants.csv")],
    }
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for table, table_frames in frames.items():
        profiled = profile_suite(table_frames, table)
        profiled["meta"] = {
            "profiled_from": f"{len(table_frames)} clean development file(s)",
            "dataset": {k: manifest.get(k) for k in ("seed", "seed_purpose", "schema_version",
                                                      "config")},
        }  # fmt: skip
        for kind, suite in (("profiled", profiled), ("suite", tighten(profiled, table))):
            path = out / f"{table}.{kind}.json"
            path.write_text(json.dumps(suite, indent=2, sort_keys=True, default=str) + "\n",
                            encoding="utf-8", newline="\n")  # fmt: skip
            written[f"{table}.{kind}"] = path
    return written


# ---- B2: the arm -----------------------------------------------------------------------------


_B2_CLASS = {
    "expect_column_values_to_not_be_null": "D03",
    "expect_column_values_to_be_in_type_list": "D04",
    "expect_column_values_to_match_regex": "D04",
}


def b2_claim(expectation: dict[str, Any], result: dict[str, Any], *, dimension: bool) -> str | None:
    """B2's defect-class claim for one failing expectation (catalogue section 4)."""
    kind = expectation["type"]
    column = expectation["kwargs"].get("column")
    tightening = expectation.get("meta", {}).get("id")
    if "error" in result:
        return "D04"  # the column's values are not of the type the check expects
    if kind == "expect_column_values_to_be_unique":
        return "D12" if dimension else "D01"
    if kind == "expect_table_columns_to_match_ordered_list":
        observed = set(result.get("observed_value") or [])
        expected = set(expectation["kwargs"]["column_list"])
        missing, unexpected = expected - observed, observed - expected
        return "D07" if missing and unexpected else "D08" if missing else None
    if tightening in ("K1", "K2"):
        return "D06"
    if tightening == "K5" or (kind == "expect_column_values_to_be_between" and column == "amount"):
        return "D05"
    if tightening == "K4":
        whole = result.get("unexpected_count") == result.get("element_count")
        return "D11" if whole else "D10"
    if tightening == "K6":
        return "D11"
    return _B2_CLASS.get(kind)


class B2Arm:
    """Arm B2: the frozen Great Expectations suites, validated on each delivery."""

    name = "B2"

    def __init__(self, suites: Path = B2_DIR) -> None:
        self._paths = {t: suites / f"{t}.suite.json" for t in ("transactions_raw", "merchants")}
        self._suites = {
            t: json.loads(p.read_text(encoding="utf-8")) for t, p in self._paths.items()
        }
        self._parameters: dict[str, Any] = {}
        self._definitions: dict[str, Any] = {}
        self._gx_suites: dict[str, Any] = {}

    def describe(self) -> dict[str, Any]:
        gx = _gx()
        return {
            "arm": self.name,
            "tool": "great_expectations",
            "version": gx.__version__,
            "suites": {p.name: _sha256(p) for p in self._paths.values()},
        }

    def open(self, workdir: Path, feed: Path) -> None:
        gx = _gx()
        from great_expectations.data_context.types.base import (
            ProgressBarsConfig,
        )
        from great_expectations.expectations.expectation_configuration import (
            ExpectationConfiguration,
        )

        context = gx.get_context(mode="ephemeral")
        context.variables.progress_bars = ProgressBarsConfig(globally=False)
        source = context.data_sources.add_pandas("sandboxbank")
        for table, suite in self._suites.items():
            asset = source.add_dataframe_asset(table)
            self._definitions[table] = asset.add_batch_definition_whole_dataframe(table)
            self._gx_suites[table] = context.suites.add(
                gx.ExpectationSuite(
                    name=table,
                    expectations=[
                        ExpectationConfiguration(
                            type=e["type"], kwargs=e["kwargs"], meta=e.get("meta", {})
                        ).to_domain_obj()
                        for e in suite["expectations"]
                    ],
                )
            )
        accounts, merchants = _read(feed / "accounts.csv"), _read(feed / "merchants.csv")
        self._parameters = {
            "account_ids": sorted(accounts["account_id"].dropna().astype(str)),
            "merchant_ids": sorted(merchants["merchant_id"].dropna().astype(str)),
            "max_txn_limit": float(accounts["txn_limit"].max()),
        }

    def _validate(
        self, table: str, path: Path, parameters: dict[str, Any]
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        frame = _read(path, table)
        batch = self._definitions[table].get_batch(batch_parameters={"dataframe": frame})
        report = batch.validate(self._gx_suites[table], expectation_parameters=parameters)
        failed = []
        for result in report.results:
            if result.success:
                continue
            config = result.expectation_config
            expectation = {"type": config.type, "kwargs": dict(config.kwargs),
                           "meta": dict(config.meta or {})}  # fmt: skip
            outcome = dict(result.result)
            if _raised(result.exception_info):
                column = expectation["kwargs"].get("column")
                if column is None or column not in frame.columns:
                    continue  # a missing column: reported once, by the column-list expectation
                outcome = {"error": "could not check (the column could not be read as expected)"}
            failed.append((expectation, outcome))
        return failed

    def observe(self, delivery: Delivery, clock: Clock) -> list[Alert]:
        if not self._definitions:
            raise RuntimeError("open() the arm before observing batches")
        start = delivery.arrival - WINDOW
        parameters = self._parameters | {
            "window_start": _timestamp(start), "window_end": _timestamp(delivery.arrival),
            "batch_ids": [delivery.batch_id],
        }  # fmt: skip
        found = [
            (e, r, False) for e, r in self._validate("transactions_raw", delivery.file, parameters)
        ]
        if delivery.dimension is not None:
            found += [(e, r, True) for e, r in self._validate("merchants", delivery.dimension, {})]
        moment = clock()
        alerts = []
        for expectation, result, dimension in found:
            kwargs = expectation["kwargs"]
            if expectation["type"] == "expect_table_columns_to_match_ordered_list":
                observed = set(result.get("observed_value") or [])
                expected = set(kwargs["column_list"])
                location = tuple(sorted((expected - observed) | (observed - expected)))
            else:
                location = (kwargs["column"],) if kwargs.get("column") else ()
            counted = (
                result["error"] if "error" in result else
                f"{result['unexpected_count']} of {result['element_count']} rows"
                if result.get("unexpected_count") is not None else
                f"observed {result.get('observed_value')}"
            )  # fmt: skip
            label = expectation["meta"].get("id") or expectation["type"]
            prefix = "merchants dimension: " if dimension else ""
            alerts.append(
                Alert(
                    arm="B2", subject_kind="batch", subject_id=delivery.batch_id, time=moment,
                    location=location, message=f"{prefix}{label} {expectation['type']}: {counted}",
                    defect_class=b2_claim(expectation, result, dimension=dimension),
                )
            )  # fmt: skip
        return alerts

    def close(self) -> None:
        self._definitions = {}
        self._gx_suites = {}


def _timestamp(moment: datetime) -> Any:
    """A bound for the parsed (UTC) timestamp column."""
    import pandas as pd

    return pd.Timestamp(moment.astimezone(UTC))


def _raised(info: Any) -> bool:
    """GX reports exceptions per metric: ``{metric id: {"raised_exception": ...}}``."""
    if not isinstance(info, dict):
        return False
    if info.get("raised_exception"):
        return True
    return any(isinstance(v, dict) and v.get("raised_exception") for v in info.values())


# ---- effort (Protocol section 3: rule counts and lines of code, per arm) ---------------------


def _code_lines(path: Path) -> int:
    """Lines that are neither blank nor comments (docstrings count: they are written too)."""
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", ";"))
    )  # fmt: skip


def effort(contracts: Path) -> dict[str, Any]:
    """What each arm's authors wrote. Harness adapters (this module, ``arms.RaiArm``) are
    shared infrastructure and not counted. Author hours are recorded by hand in REVIEW.md."""
    import importlib.util

    from rai.validation.contract import load_contract

    test_file = B1_DIR / "test_feed.py"
    loader = importlib.util.spec_from_file_location("b1_test_feed", test_file)
    if loader is None or loader.loader is None:
        raise B1Error(f"cannot load {test_file}")
    b1 = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(b1)
    b1_checks = {
        table: 2  # the column list and the primary key
        + sum(required for _, required in spec.values())
        + sum(kind != "string" for kind, _ in spec.values())
        for table, spec in b1.SPEC.items()
    }
    suites = {
        t: json.loads((B2_DIR / f"{t}.suite.json").read_text(encoding="utf-8"))
        for t in ("transactions_raw", "merchants")
    }
    sources = {t: [e["meta"]["source"] for e in s["expectations"]] for t, s in suites.items()}
    edits = [e for table in TIGHTENINGS.values() for e in table]
    return {
        "B1": {"checks": b1_checks, "code_lines": {test_file.name: _code_lines(test_file)}},
        "B2": {
            "expectations": {t: len(v) for t, v in sources.items()},
            "profiled": {t: v.count("profiled") for t, v in sources.items()},
            "hand_edits": {
                "added": sum("type" in e for e in edits),
                "replaced": sum("replaces" in e for e in edits),
                "removed": sum("removes" in e for e in edits),
            },
        },
        "R": {
            "rules": {
                p.stem: len(load_contract(p).rules()) for p in sorted(contracts.glob("*.yaml"))
            },
            "contract_lines": {p.name: _code_lines(p) for p in sorted(contracts.glob("*.yaml"))},
        },
    }
