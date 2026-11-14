"""Fixtures for API integration tests: a freshly migrated database and an API client
that logs in as rai_api. Each test module gets its own (module scope)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url

from rai.api.app import create_app
from rai.api.settings import Settings
from rai.core import db
from rai.core.accounts import set_api_role_password, set_role_password
from tests.integration.apikit import API_PASSWORD, DSN, SECRET, WORKER_PASSWORD


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
