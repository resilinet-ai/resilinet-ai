"""Fixtures for API integration tests: a freshly migrated database, an API client
that logs in as rai_api, and for run tests a SandboxBank slice and a worker engine.
Each test module gets its own (module scope)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url

from rai.api.app import create_app
from rai.api.settings import Settings
from rai.core import db
from rai.core.accounts import set_api_role_password, set_role_password
from sandboxbank.fixtures import build_slice
from tests.integration.apikit import API_PASSWORD, DSN, SECRET, WORKER_PASSWORD
from tests.integration.runkit import Lab


@pytest.fixture(scope="module")
def owner() -> Iterator[Engine]:
    db.upgrade(DSN)
    db.downgrade(DSN)  # every migration reverses cleanly
    db.upgrade(DSN)
    engine = create_engine(db.sqlalchemy_url(DSN))
    with engine.begin() as conn:
        set_api_role_password(conn, API_PASSWORD)
        set_role_password(conn, "rai_worker", WORKER_PASSWORD)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def api_url(owner: Engine) -> str:
    url = make_url(db.sqlalchemy_url(DSN)).set(username="rai_api", password=API_PASSWORD)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def client(api_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=api_url, jwt_secret=SECRET, login_attempts=3)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def worker_url(owner: Engine) -> str:
    url = make_url(db.sqlalchemy_url(DSN)).set(username="rai_worker", password=WORKER_PASSWORD)
    return url.render_as_string(hide_password=False)


# ---- runs and the worker (B-18, B-19) ----------------------------------------------------


@pytest.fixture(scope="module")
def feed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("slice")
    build_slice(
        out, data_seed=27, injector_seed=28, accounts=200, merchants=40, rows_per_batch=2_000
    )
    return out


@pytest.fixture(scope="module")
def seeded(feed: Path) -> int:
    truth = (feed / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    return len(json.loads(truth[1])["rows"])


@pytest.fixture(scope="module")
def worker_engine(worker_url: str) -> Iterator[Engine]:
    engine = create_engine(worker_url)
    yield engine
    engine.dispose()


@pytest.fixture
def lab(client: TestClient, owner: Engine) -> Lab:
    return Lab(client, owner)
