"""Protocol H3: RAI's RTO against the independent observer (B-44)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rai_bench.cli import app
from rai_bench.h3 import bland_altman, clopper_pearson, h3
from rai_bench.runner import HarnessError
from sandboxbank.observer import ObservationLog, Sample

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def write_log(path: Path, outages: list[tuple[float, float]], end: float) -> None:
    """Samples every 0.25 s from 0 to ``end``; unhealthy inside each outage."""
    log = ObservationLog(path)
    for i in range(int(end / 0.25)):
        t = i * 0.25
        down = any(start <= t < stop for start, stop in outages)
        log.append(Sample(at(t), 0 if down else 200, not down, None if down else i))


def record(injection: str, onset: float, reverted: float, rto: float | None,
           **extra: Any) -> str:  # fmt: skip
    return json.dumps({"format": "rai-bench-recovery/1", "injection": injection,
                       "onset_at": at(onset).isoformat(), "reverted_at": at(reverted).isoformat(),
                       "rto_seconds": rto, **extra})  # fmt: skip


def test_clopper_pearson_matches_published_values() -> None:
    # Reference: the beta quantiles, Beta(0.025; x, n-x+1) and Beta(0.975; x+1, n-x).
    lower, upper = clopper_pearson(162, 170)
    assert lower == pytest.approx(0.90938, abs=1e-5)
    assert upper == pytest.approx(0.97947, abs=1e-5)
    assert clopper_pearson(10, 10) == (pytest.approx(0.6915, abs=1e-4), 1.0)
    assert clopper_pearson(0, 5) == (0.0, pytest.approx(0.5218, abs=1e-4))
    with pytest.raises(ValueError, match="successes"):
        clopper_pearson(3, 2)


def test_bland_altman_reports_bias_and_limits_of_agreement() -> None:
    stats = bland_altman([0.5, -0.5, 1.0, 0.0])
    assert stats["bias"] == 0.25
    assert stats["sd"] == pytest.approx(0.645, abs=1e-3)
    assert stats["lower_limit"] == pytest.approx(-1.015, abs=1e-3)
    assert bland_altman([])["bias"] is None


def test_each_injection_is_scored_against_the_ground_truth(tmp_path: Path) -> None:
    # I1: down 10-72 s (truth 62 s); I2: down 100-140 s (truth 40 s);
    # I3: down 200-260 s (truth 60 s); I4: never restored after 300 s.
    write_log(tmp_path / "obs.csv", [(10, 72), (100, 140), (200, 260), (300, 400)], end=400)
    lines = [
        record("I1", 10, 70, 63.0),  # within 2 s
        record("I2", 100, 130, 43.5),  # 3.5 s off
        record("I3", 200, 250, None, window_seconds=120),  # RAI: not restored; truth: 60 s
        record("I4", 300, 330, None, window_seconds=60),  # neither restored: agrees
        record("I5", 500, 520, 10.0),  # after the log ends: unobserved
    ]
    (tmp_path / "rai.jsonl").write_text("\n".join(lines) + "\n")
    result = h3(tmp_path / "rai.jsonl", tmp_path / "obs.csv")
    by_id = {row["injection"]: row for row in result["injections"]}
    assert by_id["I1"]["truth_rto_seconds"] == 62.0
    assert by_id["I1"]["error_seconds"] == 1.0
    assert by_id["I1"]["agrees"] is True
    assert by_id["I2"]["error_seconds"] == 3.5
    assert by_id["I2"]["agrees"] is False
    assert by_id["I3"]["agrees"] is False
    assert "RAI did not" in by_id["I3"]["reason"]
    assert by_id["I4"]["agrees"] is True
    assert by_id["I5"]["observed"] is False
    assert by_id["I5"]["agrees"] is None
    summary = result["summary"]
    assert (summary["scored"], summary["agreed"], summary["unobserved"]) == (4, 2, 1)
    assert summary["share"] == 0.5
    assert summary["supported"] is False
    assert summary["bland_altman"]["n"] == 2


def test_h3_is_supported_only_when_the_lower_bound_clears_ninety_percent(tmp_path: Path) -> None:
    outages = [(i * 100 + 10, i * 100 + 40) for i in range(60)]
    write_log(tmp_path / "obs.csv", outages, end=6000)
    lines = [record(f"I{i}", i * 100 + 10, i * 100 + 35, 30.5) for i in range(60)]
    (tmp_path / "rai.jsonl").write_text("\n".join(lines))
    summary = h3(tmp_path / "rai.jsonl", tmp_path / "obs.csv")["summary"]
    # 60/60 agree, but the exact lower bound is 0.9404: supported.
    assert summary["agreed"] == 60
    assert summary["supported"] is True
    assert summary["ci95"][0] == pytest.approx(0.9404, abs=1e-4)
    few = "\n".join(lines[:20])  # 20/20: lower bound 0.8316, not enough
    (tmp_path / "few.jsonl").write_text(few)
    assert h3(tmp_path / "few.jsonl", tmp_path / "obs.csv")["summary"]["supported"] is False


def test_bad_inputs_are_refused(tmp_path: Path) -> None:
    write_log(tmp_path / "obs.csv", [], end=10)
    bad = {
        "not-json.jsonl": "{",
        "wrong-format.jsonl": json.dumps({"format": "x"}),
        "twice.jsonl": record("I1", 1, 2, 1.0) + "\n" + record("I1", 1, 2, 1.0),
        "naive.jsonl": record("I1", 1, 2, 1.0).replace("+00:00", ""),
        "negative.jsonl": record("I1", 1, 2, -1.0),
        "backwards.jsonl": record("I1", 5, 2, 1.0),
        "empty.jsonl": "",
    }
    for name, text in bad.items():
        (tmp_path / name).write_text(text)
        with pytest.raises(HarnessError):
            h3(tmp_path / name, tmp_path / "obs.csv")
    (tmp_path / "tampered.csv").write_text(
        (tmp_path / "obs.csv").read_text().replace(",200,", ",503,", 1)
    )
    (tmp_path / "ok.jsonl").write_text(record("I1", 1, 2, 1.0))
    with pytest.raises(HarnessError, match="hash chain"):
        h3(tmp_path / "ok.jsonl", tmp_path / "tampered.csv")


def test_the_cli_prints_the_verdict_and_writes_the_result(tmp_path: Path) -> None:
    write_log(tmp_path / "obs.csv", [(10, 72)], end=100)
    (tmp_path / "rai.jsonl").write_text(record("I1", 10, 70, 63.0))
    out = tmp_path / "h3" / "h3.json"
    result = CliRunner().invoke(
        app, ["h3", str(tmp_path / "rai.jsonl"), str(tmp_path / "obs.csv"), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "1/1 injections within +/- 2 s" in result.output
    assert json.loads(out.read_text())["summary"]["agreed"] == 1
    missing = CliRunner().invoke(app, ["h3", str(tmp_path / "none"), str(tmp_path / "obs.csv")])
    assert missing.exit_code == 2
