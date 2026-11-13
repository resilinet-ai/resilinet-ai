"""Tenant and actor context for a database transaction (NFR-SEC-1, FR-01.2).

Row-level security reads ``app.tenant_id``; the audit triggers read
``app.actor_type`` and ``app.actor_id``. All three are set transaction-locally, so
they vanish at commit or rollback and can never leak to the next request on a
pooled connection.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text

from rai.core.db import APP_ROLE

ActorType = Literal["user", "api_key", "worker", "agent", "system"]


def set_context(
    conn: Connection,
    tenant_id: UUID,
    actor_type: ActorType,
    actor_id: str,
    *,
    as_app_role: bool = True,
) -> None:
    """Scope the current transaction to one tenant and actor.

    With ``as_app_role`` the transaction also switches to ``rai_app`` so RLS applies
    even when the connection logged in as a more privileged user.
    """
    if not conn.in_transaction():
        raise RuntimeError("set_context needs an open transaction")
    if as_app_role:
        conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    conn.execute(
        text(
            "SELECT set_config('app.tenant_id', :tenant, true), "
            "set_config('app.actor_type', :actor_type, true), "
            "set_config('app.actor_id', :actor_id, true)"
        ),
        {"tenant": str(tenant_id), "actor_type": actor_type, "actor_id": actor_id},
    )
