"""Freshness probes for SandboxBank's batch jobs (B-28; the author's review, Q1).

The ETL (P2) and the feature-job (P3) have no requests to serve, so RAI's recovery
probes had nothing to ask. Each now runs a small HTTP server beside its cycles:

* ``GET /healthz``: 200 while the process runs (the container's own healthcheck).
* ``GET /readyz``: 200 only while the job is **caught up**: its last cycle succeeded,
  drained everything there was to load, and finished at most ``max_age`` ago (by
  default two minutes, or two cycles if those are longer). A failed cycle makes it 503
  at once, and so does a job that stopped finishing cycles. A paused or partitioned
  container does not answer at all, which a probe also counts as not ready.

So RTO for a P2 or P3 scenario runs until the job has caught up again, not merely until
its process is back.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MAX_AGE = 120.0  # seconds: "caught up" means within the last two minutes


class Readiness:
    """What the job last achieved, shared between its cycles and the HTTP server."""

    def __init__(self, max_age: float = DEFAULT_MAX_AGE,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._last_success: float | None = None
        self._failure: str | None = "no cycle has finished yet"

    def cycle_succeeded(self) -> None:
        """A cycle finished and drained everything available."""
        with self._lock:
            self._last_success, self._failure = self._clock(), None

    def cycle_failed(self, reason: str) -> None:
        with self._lock:
            self._failure = reason[:300] or "the cycle failed"

    def state(self) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            now = self._clock()
            age = None if self._last_success is None else now - self._last_success
            if self._failure is not None:
                return False, {"ready": False, "reason": self._failure,
                               "seconds_since_success": age}  # fmt: skip
            assert age is not None  # noqa: S101 - a success was recorded
            if age > self.max_age:
                reason = f"no finished cycle for more than {self.max_age:.0f} s"
                return False, {"ready": False, "seconds_since_success": round(age, 1),
                               "reason": reason}  # fmt: skip
            return True, {"ready": True, "seconds_since_success": round(age, 1)}


def serve(readiness: Readiness, host: str, port: int) -> ThreadingHTTPServer:
    """Start the probe server in a daemon thread and return it (``shutdown()`` stops it)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                status, body = 200, {"status": "ok"}
            elif self.path == "/readyz":
                ready, body = readiness.state()
                status = 200 if ready else 503
            else:
                status, body = 404, {"error": "not found"}
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_: Any) -> None:  # probes every 250 ms: keep the job's log clean
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
