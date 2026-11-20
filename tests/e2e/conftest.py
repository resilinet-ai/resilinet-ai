"""The live stack for the journey tests (B-46), started once per session.

Without ``RAI_E2E_DATABASE_URL`` the journeys are skipped (the ordinary test job), unless
``RAI_E2E_REQUIRED`` is set (the nightly job "Journeys"), where a missing stack fails."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.e2e.stack import Stack, started

OWNER_URL = os.environ.get("RAI_E2E_DATABASE_URL", "")
if os.environ.get("RAI_E2E_REQUIRED") and not OWNER_URL:  # never skip silently there
    raise RuntimeError("RAI_E2E_DATABASE_URL must be set when RAI_E2E_REQUIRED is")


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    if not OWNER_URL:
        pytest.skip(
            "RAI_E2E_DATABASE_URL not set: the journeys need the live stack (make journeys)"
        )
    with started(OWNER_URL, tmp_path_factory.mktemp("journeys")) as live:
        yield live
