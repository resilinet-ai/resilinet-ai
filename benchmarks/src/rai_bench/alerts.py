"""The common alert format every arm writes (Protocol sections 6 and 8, B-15).

Protocol section 8: "Each arm writes alerts in one common format: time, batch or
injection, location, message." One alert is one JSON object per line::

    {"arm": "R", "defect_class": "D02", "format": "rai-bench-alert/1",
     "location": ["txn_id"], "message": "...", "subject": {"id": "B000001", "kind": "batch"},
     "time": "2026-11-14T10:00:01.250000Z"}

- ``time``: when the arm raised the alert, UTC wall clock. Lead time is measured
  from the moment the harness delivered the batch or started the injection.
- ``subject``: the batch or the injection the alert is about.
- ``location``: the columns (for a batch) or components (for an injection) named;
  may be empty, for example for a whole-batch alert.
- ``message``: free text for the adjudicator. Arm-identifying wording is stripped
  when alerts are blinded.
- ``defect_class``: optional. The arm's own claim of which catalogue class (D01-D12)
  it saw. Every arm may make it; the matching rule accepts either a shared column
  or the same class, so a whole-batch defect without a column (D11) can still match.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

ALERT_FORMAT = "rai-bench-alert/1"
ARMS = ("B1", "B2", "R")
DEFECT_CLASSES = tuple(f"D{n:02d}" for n in range(1, 13))
SubjectKind = Literal["batch", "injection"]
_FIELDS = {"arm", "defect_class", "format", "location", "message", "subject", "time"}


class AlertError(ValueError):
    """An alert that does not follow the common format."""


def format_time(moment: datetime) -> str:
    """UTC with microseconds and a Z suffix: sortable as text, unambiguous."""
    if moment.tzinfo is None:
        raise AlertError("alert time must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class Alert:
    arm: str
    subject_kind: SubjectKind
    subject_id: str
    time: datetime
    location: tuple[str, ...]
    message: str
    defect_class: str | None = None

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise AlertError(f"arm must be one of {', '.join(ARMS)}, not {self.arm!r}")
        if self.subject_kind not in ("batch", "injection"):
            raise AlertError(f"subject kind must be batch or injection, not {self.subject_kind!r}")
        if not self.subject_id:
            raise AlertError("subject id must not be empty")
        if self.time.tzinfo is None:
            raise AlertError("alert time must be timezone-aware")
        if any(not isinstance(item, str) or not item for item in self.location):
            raise AlertError("location entries must be non-empty strings")
        if self.defect_class is not None and self.defect_class not in DEFECT_CLASSES:
            raise AlertError(f"defect_class must be D01-D12 or null, not {self.defect_class!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": ALERT_FORMAT,
            "arm": self.arm,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "time": format_time(self.time),
            "location": list(self.location),
            "message": self.message,
            "defect_class": self.defect_class,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Alert:
        unknown = set(data) - _FIELDS
        if unknown:
            raise AlertError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if data.get("format") != ALERT_FORMAT:
            raise AlertError(f"format must be {ALERT_FORMAT!r}")
        subject = data.get("subject")
        if not isinstance(subject, Mapping) or set(subject) != {"kind", "id"}:
            raise AlertError("subject must be an object with exactly kind and id")
        location = data.get("location")
        if not isinstance(location, list):
            raise AlertError("location must be a list of strings")
        for name in ("arm", "message", "time"):
            if not isinstance(data.get(name), str):
                raise AlertError(f"{name} must be a string")
        if not isinstance(subject["id"], str):
            raise AlertError("subject id must be a string")
        try:
            moment = datetime.fromisoformat(data["time"].replace("Z", "+00:00"))
        except ValueError:
            raise AlertError(f"time {data['time']!r} is not ISO 8601") from None
        if moment.tzinfo is None:
            raise AlertError("alert time must be timezone-aware")
        defect_class = data.get("defect_class")
        if defect_class is not None and not isinstance(defect_class, str):
            raise AlertError("defect_class must be a string or null")
        return cls(
            arm=data["arm"],
            subject_kind=cast(SubjectKind, subject["kind"]),
            subject_id=subject["id"],
            time=moment,
            location=tuple(location),
            message=data["message"],
            defect_class=defect_class,
        )


def dumps(alert: Alert) -> str:
    return json.dumps(alert.to_dict(), sort_keys=True, ensure_ascii=False)


def write_alerts(path: Path, alerts: Iterable[Alert]) -> int:
    """Write alerts as JSON Lines (LF endings); returns how many were written."""
    lines = [dumps(alert) for alert in alerts]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    return len(lines)


def read_alerts(path: Path) -> list[Alert]:
    """Read and validate a JSON Lines alert file; errors name the line."""
    alerts: list[Alert] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise AlertError("each line must be a JSON object")
            alerts.append(Alert.from_dict(data))
        except (json.JSONDecodeError, AlertError) as error:
            raise AlertError(f"{path.name} line {number}: {error}") from None
    return alerts
