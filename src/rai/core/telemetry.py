"""Structured logs and trace context (NFR-OPS-1), with the standard library only.

* **Logs** are JSON lines on stderr: ``time`` (UTC), ``level``, ``logger``,
  ``service``, ``message``, the active ``trace_id`` and ``span_id``, any fields a
  call passes as ``extra={"fields": {...}}``, and ``error`` with the stack for an
  exception. ``RAI_LOG_FORMAT=text`` gives readable lines for a terminal instead.
* **Traces** follow W3C Trace Context. A span is a unit of work (an HTTP request,
  a run on the worker); it continues the caller's ``traceparent`` when there is
  one, and is written as one log line when it ends (``"event": "span"`` with its
  parent, duration and status). Any log pipeline can rebuild the trace from them;
  no exporter or collector is needed.

Never log secrets, tokens, request bodies or personal data: log ids and counts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import time
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

LOG_FORMAT_ENV = "RAI_LOG_FORMAT"
LOG_LEVEL_ENV = "RAI_LOG_LEVEL"
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message", "asctime", "fields",
    "color_message",  # uvicorn's copy of the message with terminal colour codes
}  # fmt: skip


@dataclass
class Span:
    """One unit of work in a trace. ``attributes`` are logged when it ends."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def traceparent(self) -> str:
        """This span as a W3C ``traceparent`` header (sampled)."""
        return f"00-{self.trace_id}-{self.span_id}-01"


_current: ContextVar[Span | None] = ContextVar("rai_span", default=None)


def current_span() -> Span | None:
    return _current.get()


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """``(trace_id, parent_span_id)`` from a W3C ``traceparent``, or None if it is
    absent or malformed (all-zero ids are invalid). A bad header starts a new trace."""
    if not header:
        return None
    match = _TRACEPARENT.fullmatch(header.strip().lower())
    if match is None or match[1] == "0" * 32 or match[2] == "0" * 16:
        return None
    return match[1], match[2]


@contextmanager
def span(name: str, *, traceparent: str | None = None, **attributes: Any) -> Iterator[Span]:
    """Run a block as a span: a child of the current span, else of ``traceparent``,
    else the root of a new trace. Logged once, when the block ends."""
    parent = _current.get()
    if parent is not None:
        trace_id, parent_id = parent.trace_id, parent.span_id
    elif (given := parse_traceparent(traceparent)) is not None:
        trace_id, parent_id = given
    else:
        trace_id, parent_id = secrets.token_hex(16), None
    this = Span(name, trace_id, secrets.token_hex(8), parent_id, dict(attributes))
    token = _current.set(this)
    started = time.perf_counter()
    try:
        yield this
    except BaseException:
        this.status = "error"
        raise
    finally:
        duration = round((time.perf_counter() - started) * 1000, 3)
        logging.getLogger("rai.trace").info(
            "%s %s in %.1f ms", name, this.status, duration,
            extra={"fields": {"event": "span", "span": name, "parent_span_id": parent_id,
                              "duration_ms": duration, "status": this.status, **this.attributes}},
        )  # fmt: skip
        _current.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line; keys sorted, so lines diff and grep well."""

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
        active = _current.get()
        if active is not None:
            entry["trace_id"], entry["span_id"] = active.trace_id, active.span_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, Mapping):
            entry.update({k: v for k, v in fields.items() if k not in entry})  # never overrides
        entry.update(
            {k: v for k, v in record.__dict__.items() if k not in _RESERVED and k not in entry}
        )
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            entry["error"] = {"type": type(error).__name__, "message": str(error)}
            entry["stack"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str, sort_keys=True, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Readable lines for a terminal, with the trace id when there is one."""

    def format(self, record: logging.LogRecord) -> str:
        active = _current.get()
        trace = f" [{active.trace_id[:8]}]" if active is not None else ""
        stamp = self.formatTime(record)
        line = f"{stamp} {record.levelname} {record.name}{trace} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + "".join(traceback.format_exception(*record.exc_info))
        return line


def configure_logging(service: str, environ: Mapping[str, str] | None = None) -> None:
    """Send every log record to stderr as JSON (or text, with ``RAI_LOG_FORMAT=text``),
    at ``RAI_LOG_LEVEL`` (default INFO). Replaces any handlers already installed."""
    env = os.environ if environ is None else environ
    form = env.get(LOG_FORMAT_ENV, "json").strip().lower()
    if form not in ("json", "text"):
        raise ValueError(f"{LOG_FORMAT_ENV} must be json or text, not {form!r}")
    level = env.get(LOG_LEVEL_ENV, "INFO").strip().upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ValueError(f"{LOG_LEVEL_ENV} must be DEBUG, INFO, WARNING or ERROR, not {level!r}")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(service) if form == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
