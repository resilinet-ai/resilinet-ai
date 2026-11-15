"""Failed-login limiter (threats S-01, D-03) and the per-API-key request limit (B-20).

Counts failures per key (the lower-cased email, and separately the client
address) in a sliding window. Unknown emails are counted exactly like known ones,
so the 429 does not reveal which accounts exist. In-process only: correct for one
API process, which is the MVP deployment; a shared store comes with scaling out.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class FailureLimiter:
    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit, self._window, self._clock = limit, window_seconds, clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        failures = self._failures[key]
        while failures and failures[0] <= now - self._window:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return deque()
        return failures

    def retry_after(self, *keys: str) -> int | None:
        """Seconds to wait if any key is over the limit, else None."""
        now = self._clock()
        with self._lock:
            waits = []
            for key in keys:
                failures = self._prune(key, now)
                if len(failures) >= self._limit:
                    waits.append(failures[0] + self._window - now)
        return max(1, math.ceil(max(waits))) if waits else None

    def fail(self, *keys: str) -> None:
        now = self._clock()
        with self._lock:
            for key in keys:
                self._failures[key].append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class RequestLimiter:
    """At most ``limit`` requests per key in a sliding window (API keys, threat D-03)."""

    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._failures = FailureLimiter(limit, window_seconds, clock)

    def take(self, key: str) -> int | None:
        """Count one request; returns the seconds to wait instead if the key is over."""
        wait = self._failures.retry_after(key)
        if wait is None:
            self._failures.fail(key)
        return wait
