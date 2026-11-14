"""The common alert format (B-15, Protocol section 8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from rai_bench.alerts import (
    ALERT_FORMAT,
    Alert,
    AlertError,
    format_time,
    read_alerts,
    write_alerts,
)

MOMENT = datetime(2026, 11, 14, 10, 0, 1, 250_000, tzinfo=UTC)


def alert(**overrides: Any) -> Alert:
    fields: dict[str, Any] = {
        "arm": "R",
        "subject_kind": "batch",
        "subject_id": "B000001",
        "time": MOMENT,
        "location": ("txn_id",),
        "message": "unique_across_batches:txn_id (critical): 100 of 100100 rows fail",
        "defect_class": "D02",
    }
    return Alert(**(fields | overrides))


def test_alert_round_trips_through_its_documented_shape(tmp_path: Path) -> None:
    original = alert(
        time=datetime(2026, 11, 14, 12, 0, 1, 250_000, tzinfo=timezone(timedelta(hours=2)))
    )
    assert original.to_dict() == {
        "format": ALERT_FORMAT,
        "arm": "R",
        "subject": {"kind": "batch", "id": "B000001"},
        "time": "2026-11-14T10:00:01.250000Z",  # always UTC
        "location": ["txn_id"],
        "message": original.message,
        "defect_class": "D02",
    }
    path = tmp_path / "alerts" / "R.jsonl"
    assert write_alerts(path, [original, alert(defect_class=None, location=())]) == 2
    assert b"\r\n" not in path.read_bytes()
    first, second = read_alerts(path)
    assert first == alert()
    assert (second.defect_class, second.location) == (None, ())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"arm": "RAI"}, "arm must be one of B1, B2, R"),
        ({"subject_kind": "run"}, "subject kind must be batch or injection"),
        ({"subject_id": ""}, "subject id must not be empty"),
        ({"time": datetime(2026, 11, 14)}, "timezone-aware"),
        ({"location": ("",)}, "non-empty strings"),
        ({"defect_class": "D13"}, "D01-D12 or null"),
    ],
)
def test_invalid_alerts_are_refused(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(AlertError, match=message):
        alert(**overrides)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "unknown field"),
        ({"format": "other/1"}, "format must be"),
        ({"subject": {"kind": "batch"}}, "exactly kind and id"),
        ({"subject": {"kind": "batch", "id": 7}}, "subject id must be a string"),
        ({"location": "txn_id"}, "location must be a list"),
        ({"message": None}, "message must be a string"),
        ({"time": "yesterday"}, "not ISO 8601"),
        ({"time": "2026-11-14T10:00:00"}, "timezone-aware"),
        ({"defect_class": 2}, "string or null"),
    ],
)
def test_alert_files_are_checked_line_by_line(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    good = alert().to_dict()
    path = tmp_path / "R.jsonl"
    path.write_text(json.dumps(good) + "\n\n" + json.dumps(good | change) + "\n", "utf-8")
    with pytest.raises(AlertError, match=f"R.jsonl line 3: .*{message}"):
        read_alerts(path)


def test_alert_times_must_carry_a_zone() -> None:
    assert format_time(MOMENT) == "2026-11-14T10:00:01.250000Z"
    with pytest.raises(AlertError, match="timezone-aware"):
        format_time(datetime(2026, 11, 14))


def test_non_object_lines_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "R.jsonl"
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(AlertError, match="line 1: each line must be a JSON object"):
        read_alerts(path)
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(AlertError, match="line 1"):
        read_alerts(path)
