"""UUIDv7 identifiers (RFC 9562): time-ordered and index-friendly primary keys.

Python 3.12 has no ``uuid.uuid7``; PostgreSQL 16 has no ``uuidv7()``. This module
and the SQL function ``rai.uuid_v7()`` produce the same layout.
"""

from __future__ import annotations

import os
import time
import uuid

_VERSION_7 = 0x7 << 76
_VARIANT_RFC = 0b10 << 62


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    """A version-7 UUID: 48-bit Unix milliseconds followed by 74 random bits."""
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("unix_ms must fit in 48 bits")
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms << 80) | _VERSION_7 | (rand_a << 64) | _VARIANT_RFC | rand_b
    return uuid.UUID(int=value)


def uuid7_unix_ms(value: uuid.UUID) -> int:
    """The millisecond timestamp embedded in a UUIDv7."""
    if value.version != 7:
        raise ValueError("not a version-7 UUID")
    return value.int >> 80
