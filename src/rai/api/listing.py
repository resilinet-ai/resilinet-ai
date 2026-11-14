"""Shared API conventions (Data Model section 7): cursor pagination and ETags.

* Lists take ``?limit=50&cursor=<opaque>`` (limit at most 200) and answer
  ``{"items": [...], "next_cursor": ...}``. The cursor is the last item's sort key,
  base64url-encoded; clients must treat it as opaque.
* Mutable resources answer with an ``ETag``; an update must send it back in
  ``If-Match``: missing is 428, stale is 412.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Header, Query
from pydantic import BaseModel

from rai.api.problems import Problem

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class PageOut[T](BaseModel):
    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class Page:
    limit: int
    after: str | None  # decoded sort key of the last item already seen


def encode_cursor(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")


def _invalid_cursor() -> Problem:
    return Problem(400, "request.invalid_cursor", "The cursor is not valid")


def _decode(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        # validate=True: characters outside the alphabet are an error, not skipped.
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        key = raw.decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        raise _invalid_cursor() from None
    if not key:
        raise _invalid_cursor()
    return key


def after_uuid(wanted: Page) -> UUID | None:
    """The cursor as a UUID, for lists ordered by their UUIDv7 key."""
    if wanted.after is None:
        return None
    try:
        return UUID(wanted.after)
    except ValueError:
        raise _invalid_cursor() from None


def after_int(wanted: Page) -> int:
    """The cursor as a number, for lists ordered by version (0 = from the start)."""
    if wanted.after is None:
        return 0
    if not wanted.after.isdigit():
        raise _invalid_cursor()
    return int(wanted.after)


def page(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Page:
    """Dependency: the page the client asked for."""
    return Page(limit, _decode(cursor) if cursor else None)


def paged[T](rows: list[T], wanted: Page, key_of: Callable[[T], str]) -> PageOut[T]:
    """``rows`` was fetched with ``LIMIT wanted.limit + 1``: one extra row says there
    is a next page, whose cursor is the sort key of this page's last item."""
    items = rows[: wanted.limit]
    more = len(rows) > wanted.limit
    return PageOut[T](items=items, next_cursor=encode_cursor(key_of(items[-1])) if more else None)


def etag(row_version: int) -> str:
    return f'"v{row_version}"'


def if_match(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int:
    """Dependency for updates: the row_version the client last saw."""
    if if_match is None:
        raise Problem(428, "request.if_match_required", "Send If-Match with the ETag you last read")
    tag = if_match.strip().removeprefix("W/")
    if not (tag.startswith('"v') and tag.endswith('"') and tag[2:-1].isdigit()):
        raise Problem(412, "request.stale", "The resource has changed; read it again")
    return int(tag[2:-1])
