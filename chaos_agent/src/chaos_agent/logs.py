"""JSON log lines for the agent and the watchdog (NFR-OPS-1), standard library only.

The same shape as RAI's own logs (``rai.core.telemetry``), which this package must
not import: ``time`` (UTC), ``level``, ``logger``, ``service``, ``message``, any
``extra={"fields": {...}}``, and ``error`` with the stack for an exception.
``RAI_LOG_FORMAT=text`` gives readable lines for a terminal.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "service": self.service,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            entry.update({k: v for k, v in fields.items() if k not in entry})
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
            entry["stack"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str, sort_keys=True, ensure_ascii=False)


def configure(service: str, environ: Mapping[str, str] | None = None) -> None:
    """JSON lines on stderr at INFO, or text with ``RAI_LOG_FORMAT=text``."""
    env = os.environ if environ is None else environ
    handler = logging.StreamHandler(sys.stderr)
    if env.get("RAI_LOG_FORMAT", "json").strip().lower() == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
