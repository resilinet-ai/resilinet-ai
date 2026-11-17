"""Read-only checks of allow-listed targets during a scenario (B-26b: FR-08.1, FR-08.2).

The URLs come from the environment's allow-list (``health_url``, registered by an
admin, B-22), never from the scenario: a scenario names targets, not addresses.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 2.0


def get(url: str, timeout: float = TIMEOUT) -> tuple[int, Any]:
    """Status and JSON body (None if not JSON); status 0 if the target did not answer."""
    request = urllib.request.Request(  # noqa: S310 - an allow-listed http(s) URL
        url, headers={"User-Agent": "rai-worker-probe", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(64 * 1024)
            status = response.status
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        return status, None


def healthy(url: str, timeout: float = TIMEOUT) -> bool:
    return 200 <= get(url, timeout)[0] < 300
