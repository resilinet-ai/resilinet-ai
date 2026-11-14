"""Benchmark harness: runner, RAI arm, scoring and CLI (B-15, Protocol sections 6-9)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rai.validation.engine import RuleResult
from rai_bench import environment as environment_module
from rai_bench.alerts import Alert, read_alerts, write_alerts
from rai_bench.arms import Delivery, RaiArm, claimed_class
from rai_bench.cli import app
from rai_bench.runner import HarnessError, run
from rai_bench.scale import history_scale
from rai_bench.scoring import Instance, matches, percentile, score
from sandboxbank.fixtures import build_fixtures

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts" / "sandboxbank"
CLASSES = [f"D{n:02d}" for n in range(1, 13)]
runner = CliRunner()


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("fixtures")
    build_fixtures(
        out,
        data_seed=61,
        injector_seed=62,
        accounts=200,
        merchants=40,
        rows_per_batch=1_500,
        batches_per_class=1,
        clean=2,
    )
    return out


@pytest.fixture(scope="module")
def scored(fixtures: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("run") / "run"
    run(fixtures, out, [RaiArm(CONTRACTS)], repo_root=ROOT)
    return out, score(fixtures, out)


def feed_only(fixtures: Path, target: Path) -> Path:
    """A copy of the fixture set without its answer key."""
    shutil.copytree(fixtures, target, ignore=shutil.ignore_patterns("answer_key"))
    return target


# ---- the whole chain on a sealed development fixture set --------------------------------


def test_rai_arm_finds_every_seeded_class_without_false_alarms(
    scored: tuple[Path, dict[str, Any]],
) -> None:
    _, scores = scored
    arm = scores["arms"]["R"]
    assert (scores["instances"], scores["clean_controls"]) == (12, 2)
    assert arm["recall"] == {"numerator": 12, "denominator": 12, "value": 1.0}
    assert sorted(arm["recall_by_class"]) == CLASSES
    assert arm["false_alarms_on_clean_controls"]["numerator"] == 0
    precision = arm["precision_provisional"]
    assert 0 < precision["numerator"] <= precision["denominator"] == arm["alerts"]
    assert arm["latency_wall_seconds"]["n"] == 14
    assert arm["lead_time_seconds"]["n"] == 12
    assert arm["lead_time_seconds"]["p50"] >= 0


def test_run_files_record_what_the_protocol_needs(
    fixtures: Path, scored: tuple[Path, dict[str, Any]]
) -> None:
    out, _ = scored
    info = json.loads((out / "run.json").read_text("utf-8"))
    manifest = json.loads((fixtures / "manifest.json").read_text("utf-8"))
    assert info["fixtures"]["answer_key_sha256"] == manifest["answer_key_sha256"]
    assert info["fixtures"]["name"] == fixtures.name  # a name, never a local path
    assert str(fixtures.parent) not in (out / "run.json").read_text("utf-8")
    assert info["arms"][0]["arm"] == "R"
    assert set(info["arms"][0]["contracts"]) == {"transactions_raw@1", "merchants@1"}
    env = info["environment"]
    assert len(env["lockfile_sha256"]) == 64
    assert set(env["versions"]) == {"rai", "rai-bench", "rai-sandboxbank", "polars"}
    deliveries = (out / "deliveries.jsonl").read_text("utf-8").splitlines()
    assert len(deliveries) == 14
    assert {"delivered_at", "wall_seconds", "cpu_seconds"} <= set(json.loads(deliveries[0]))
    alerts = read_alerts(out / "alerts" / "R.jsonl")
    labels = (out / "auto_labels.jsonl").read_text("utf-8").splitlines()
    assert len(labels) == len(alerts)


def test_the_runner_never_needs_the_answer_key(fixtures: Path, tmp_path: Path) -> None:
    feed = feed_only(fixtures, tmp_path / "fx")
    summary = run(feed, tmp_path / "run", [RaiArm(CONTRACTS)])
    assert summary.batches == 14
    with pytest.raises(HarnessError, match="does not match its SEAL"):
        score(feed, tmp_path / "run")


def test_evaluation_fixtures_need_explicit_permission(fixtures: Path, tmp_path: Path) -> None:
    feed = feed_only(fixtures, tmp_path / "fx")
    manifest = json.loads((feed / "manifest.json").read_text("utf-8"))
    (feed / "manifest.json").write_text(json.dumps(manifest | {"seed_purpose": "evaluation"}))
    with pytest.raises(HarnessError, match="evaluation fixtures"):
        run(feed, tmp_path / "a", [RaiArm(CONTRACTS)])
    assert run(feed, tmp_path / "b", [RaiArm(CONTRACTS)], allow_evaluation=True).batches == 14
    (feed / "manifest.json").write_text(json.dumps(manifest | {"seed_purpose": "other"}))
    with pytest.raises(HarnessError, match="unknown seed purpose"):
        run(feed, tmp_path / "c", [RaiArm(CONTRACTS)])


def test_runner_refuses_untrustworthy_inputs(fixtures: Path, tmp_path: Path) -> None:
    arms = [RaiArm(CONTRACTS)]
    with pytest.raises(HarnessError, match=r"no manifest\.json"):
        run(tmp_path, tmp_path / "out", arms)
    with pytest.raises(HarnessError, match="at least one arm, each at most once"):
        run(fixtures, tmp_path / "out", [*arms, *arms])
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "x").write_text("x")
    with pytest.raises(HarnessError, match="never overwritten"):
        run(fixtures, tmp_path / "used", arms)

    feed = feed_only(fixtures, tmp_path / "fx")
    batch = feed / "feed" / "transactions_raw" / "B000001.csv"
    batch.write_bytes(batch.read_bytes() + b"\n")
    with pytest.raises(HarnessError, match="does not match its manifest hash"):
        run(feed, tmp_path / "out", arms)
    batch.unlink()
    with pytest.raises(HarnessError, match="missing"):
        run(feed, tmp_path / "out", arms)

    empty = feed_only(fixtures, tmp_path / "empty")
    manifest = json.loads((empty / "manifest.json").read_text("utf-8"))
    (empty / "manifest.json").write_text(json.dumps(manifest | {"files": {}}))
    for path in (empty / "feed" / "transactions_raw").iterdir():
        path.rename(path.with_suffix(".old"))
    (empty / "feed" / "transactions_raw" / "Bnotes.csv").write_text("x")  # not a batch name
    with pytest.raises(HarnessError, match="no batches"):
        run(empty, tmp_path / "out", arms)


def test_scoring_refuses_mismatched_or_incomplete_runs(
    fixtures: Path, scored: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    out, _ = scored
    with pytest.raises(HarnessError, match=r"no run\.json"):
        score(fixtures, tmp_path)
    other = tmp_path / "other"
    build_fixtures(
        other, data_seed=63, injector_seed=64, accounts=50, merchants=10,
        rows_per_batch=200, batches_per_class=1, clean=2,
    )  # fmt: skip
    with pytest.raises(HarnessError, match="different fixture set"):
        score(other, out)

    copy = tmp_path / "copy"
    shutil.copytree(out, copy)
    alerts = read_alerts(copy / "alerts" / "R.jsonl")
    write_alerts(copy / "alerts" / "R.jsonl", [*alerts, _alert("B2", "B000001", ())])
    with pytest.raises(HarnessError, match="alerts of another arm"):
        score(fixtures, copy)
    (copy / "deliveries.jsonl").unlink()
    with pytest.raises(HarnessError, match=r"no deliveries\.jsonl"):
        score(fixtures, copy)


# ---- the matching rule and metric helpers ------------------------------------------------


def _alert(arm: str, batch: str, location: tuple[str, ...], cls: str | None = None) -> Alert:
    return Alert(arm, "batch", batch, datetime(2026, 1, 1, tzinfo=UTC), location, "m", cls)


def test_matching_rule_needs_the_batch_and_a_column_or_the_class() -> None:
    seeded = Instance("B000003", "D11", frozenset())
    renamed = Instance("B000004", "D07", frozenset({"status"}))
    assert matches(_alert("R", "B000003", ("txn_id",), "D11"), seeded)
    assert not matches(_alert("R", "B000003", ("txn_id",), "D02"), seeded)
    assert matches(_alert("R", "B000004", ("status", "state")), renamed)
    assert not matches(_alert("R", "B000005", ("status",), "D07"), renamed)  # other batch
    injection = Alert("R", "injection", "B000004", datetime(2026, 1, 1, tzinfo=UTC), (), "m", "D07")
    assert not matches(injection, renamed)


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([float(n) for n in range(1, 101)], 0.95) == 95.0
    assert percentile([7.0], 0.95) == 7.0


def _result(kind: str, outcome: str = "fail", failing: int = 1, rows: int = 10) -> RuleResult:
    return RuleResult(f"x.{kind}", kind, ("x",), "critical", outcome, rows, failing)  # type: ignore[arg-type]


def test_rai_arm_class_claims() -> None:
    assert claimed_class(_result("unique_across_batches"), dimension=False) == "D02"
    assert claimed_class(_result("unique_across_batches", failing=10), dimension=False) == "D11"
    assert claimed_class(_result("freshness"), dimension=False) == "D10"
    assert claimed_class(_result("freshness", failing=10), dimension=False) == "D11"
    assert claimed_class(_result("max_lookup"), dimension=False) == "D05"
    assert claimed_class(_result("pattern"), dimension=False) is None
    assert claimed_class(_result("type", outcome="error"), dimension=False) is None
    assert claimed_class(_result("primary_key"), dimension=True) == "D12"
    assert claimed_class(_result("required"), dimension=True) is None


def test_rai_arm_must_be_opened_first(fixtures: Path) -> None:
    delivery = Delivery("B000000", fixtures, fixtures, datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="open"):
        RaiArm(CONTRACTS).observe(delivery, lambda: datetime.now(UTC))


def test_environment_without_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(environment_module.shutil, "which", lambda _: None)
    env = environment_module.environment(tmp_path)
    assert (env["git_commit"], env["git_dirty"], env["lockfile_sha256"]) == (None, None, None)
    monkeypatch.setattr(environment_module.shutil, "which", lambda _: str(tmp_path / "no-git"))
    assert environment_module.environment(tmp_path)["git_commit"] is None


# ---- cross-batch history at protocol size (scaled down here) ------------------------------


def test_history_scale_finds_every_planted_key(tmp_path: Path) -> None:
    result = history_scale(
        tmp_path / "scale", CONTRACTS, keys=5_000, planted=50, repeats=2, rows=2_000
    )
    assert (result["history_keys"], result["found"], result["planted"]) == (5_000, 50, 50)
    assert len(result["validate_seconds"]) == 2
    assert result["within_budget"] is True
    with pytest.raises(HarnessError, match="not empty"):
        history_scale(tmp_path / "scale", CONTRACTS, keys=10)
    with pytest.raises(HarnessError, match="planted"):
        history_scale(tmp_path / "new", CONTRACTS, keys=10, planted=20)


# ---- CLI -------------------------------------------------------------------------------------


def test_cli_run_score_and_dry_run(fixtures: Path, tmp_path: Path) -> None:
    contracts = ["--contracts", str(CONTRACTS)]
    out = tmp_path / "run"
    result = runner.invoke(app, ["run", str(fixtures), "--out", str(out), *contracts])
    assert result.exit_code == 0, result.output
    assert "14 batches delivered; alerts: R" in result.output
    result = runner.invoke(app, ["score", str(fixtures), str(out)])
    assert result.exit_code == 0, result.output
    assert "arm R: recall 12/12" in result.output
    assert "classes missed at least once: none" in result.output

    dry = tmp_path / "dry"
    result = runner.invoke(app, ["dry-run", "--out", str(dry), "--rows", "1500", *contracts])
    assert result.exit_code == 0, result.output
    assert "15 batches delivered to arm R" in result.output
    assert (dry / "run" / "scores.json").is_file()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "{fx}", "--out", "{tmp}/a", "--arm", "B1"], "not available yet"),
        (["run", "{fx}", "--out", "{tmp}/a", "--contracts", "{tmp}"], "could not load the RAI"),
        (["run", "{tmp}", "--out", "{tmp}/a"], "no manifest.json"),
        (["score", "{fx}", "{tmp}"], "no run.json"),
        (["dry-run", "--out", "{tmp}"], "never overwritten"),
        (["history-scale", "--out", "{tmp}"], "not empty"),
    ],
)
def test_cli_problems_exit_2(fixtures: Path, tmp_path: Path, args: list[str], message: str) -> None:
    (tmp_path / "keep").write_text("x")
    argv = [a.format(fx=fixtures, tmp=tmp_path) for a in args]
    result = runner.invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert message in result.output
