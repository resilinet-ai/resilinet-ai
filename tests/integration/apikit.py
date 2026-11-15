"""Helpers for API integration tests: tenants, sign-in, invites (synthetic accounts)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rai.core.accounts import create_tenant, set_password
from rai.core.passwords import hash_password

DSN = os.environ.get("RAI_TEST_DSN", "")
if os.environ.get("CI") == "true" and not DSN:  # never skip silently in CI
    raise RuntimeError("RAI_TEST_DSN must be set in CI")
needs_db = pytest.mark.skipif(not DSN, reason="RAI_TEST_DSN not set")

API_PASSWORD = "api-role-test-password"  # noqa: S105 - throwaway test database
WORKER_PASSWORD = "worker-role-test-password"  # noqa: S105 - throwaway test database
SECRET = secrets.token_urlsafe(48)  # a fresh signing key per test run
PASSWORD = "a long test password"  # noqa: S105 - synthetic test account


@dataclass(frozen=True)
class Tenant:
    tenant_id: UUID
    admin_id: UUID
    admin_email: str


def email() -> str:
    return f"{uuid4().hex[:12]}@example.test"


def new_tenant(owner: Engine, admin_email: str | None = None) -> Tenant:
    address = admin_email or email()
    slug = f"t-{uuid4().hex[:12]}"
    with owner.begin() as conn:
        made = create_tenant(
            conn, name=slug, slug=slug, admin_email=address, admin_name="Admin",
            password_hash=hash_password(PASSWORD),
        )  # fmt: skip
    return Tenant(made.tenant_id, made.admin_user_id, address)


def login(client: TestClient, address: str, password: str = PASSWORD) -> dict[str, Any]:
    response = client.post("/v1/auth/login", json={"email": address, "password": password})
    assert response.status_code == 200, response.text
    tokens: dict[str, Any] = response.json()
    return tokens


def auth(tokens: dict[str, Any], tenant: UUID | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    if tenant is not None:
        headers["X-RAI-Tenant"] = str(tenant)
    return headers


def member(client: TestClient, admin: Tenant, role: str) -> str:
    """Invite a new account with ``role`` into the admin's tenant; returns its email."""
    address = email()
    headers = auth(login(client, admin.admin_email), admin.tenant_id)
    body = {"email": address, "display_name": role.title(), "role": role}
    assert client.post("/v1/members", json=body, headers=headers).status_code == 202
    return address


def activate(owner: Engine, address: str) -> None:
    with owner.begin() as conn:
        assert set_password(conn, email=address, password_hash=hash_password(PASSWORD))


def signed_in_member(client: TestClient, owner: Engine, admin: Tenant, role: str) -> dict[str, str]:
    """Headers for a new, active member with ``role`` in the admin's tenant."""
    address = member(client, admin, role)
    activate(owner, address)
    return auth(login(client, address), admin.tenant_id)
