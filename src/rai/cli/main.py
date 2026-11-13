"""Entry point for the ``rai`` command.

Commands are added backlog item by backlog item; ``validate`` arrives with B-14.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console

from rai import __version__

app = typer.Typer(
    name="rai",
    help="Resilinet-AI: sandbox-only data-integrity and recovery assurance.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(
    help="Database schema migrations (needs RAI_DATABASE_URL).", no_args_is_help=True
)
audit_app = typer.Typer(help="Audit hash chain (needs RAI_DATABASE_URL).", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(audit_app, name="audit")
console = Console()
errors = Console(stderr=True)


@app.callback()
def main() -> None:
    """Resilinet-AI command-line interface."""


@app.command()
def version() -> None:
    """Print the RAI version."""
    console.print(f"rai {__version__}")


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision")] = "head",
) -> None:
    """Apply migrations up to REVISION."""
    from rai.core import db

    try:
        db.upgrade(revision=revision)
    except RuntimeError as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from error
    console.print(f"rai-db upgraded to {revision}")


@audit_app.command("verify")
def audit_verify(
    tenant: Annotated[UUID, typer.Option(help="Tenant ID whose chain to verify")],
) -> None:
    """Recompute a tenant's audit chain outside the database; exit 1 on the first break."""
    from sqlalchemy import create_engine

    from rai.core.audit import verify_tenant
    from rai.core.db import database_url
    from rai.core.tenancy import set_context

    try:
        url = database_url()
    except RuntimeError as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from error
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            set_context(conn, tenant, "system", "rai-cli")
            result = verify_tenant(conn, tenant)
    finally:
        engine.dispose()
    if result.ok:
        console.print(
            f"audit chain OK: {result.checked} records, head {result.head_hash} "
            "(tamper-evident, not tamper-proof)"
        )
        return
    errors.print(
        f"audit chain BROKEN at seq {result.first_break_seq} "
        f"after {result.checked} records: {result.reason}"
    )
    raise typer.Exit(1)
