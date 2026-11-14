"""``rai admin`` (operator, schema-owner credentials) and ``rai serve`` (B-16).

Passwords come from an environment variable (for automation) or a hidden prompt,
never from a command-line argument, which would end up in shell history.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer
from rich.console import Console
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError

from rai.core.db import database_url

admin_app = typer.Typer(
    help="Operator tasks: tenants, passwords (needs RAI_DATABASE_URL, the owner).",
    no_args_is_help=True,
)
console = Console(soft_wrap=True)
errors = Console(stderr=True, soft_wrap=True)

NEW_PASSWORD_ENV = "RAI_NEW_PASSWORD"  # noqa: S105 - a variable name, not a secret
API_DB_PASSWORD_ENV = "RAI_API_DB_PASSWORD"  # noqa: S105 - a variable name


def owner_engine() -> Engine:
    try:
        return create_engine(database_url())
    except RuntimeError as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from None


def read_password(env: str, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    entered: str = typer.prompt(prompt, hide_input=True, confirmation_prompt=True)
    return entered


def new_password_hash() -> str:
    from rai.core.passwords import WeakPassword, hash_password

    try:
        return hash_password(read_password(NEW_PASSWORD_ENV, "New password"))
    except WeakPassword as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from None


@admin_app.command("create-tenant")
def create_tenant_command(
    slug: Annotated[str, typer.Option(help="Short unique name, e.g. demo-cu")],
    name: Annotated[str, typer.Option(help="Display name")],
    admin_email: Annotated[str, typer.Option(help="First admin's email")],
    admin_name: Annotated[str, typer.Option(help="First admin's display name")],
) -> None:
    """Create a tenant and its first admin. A new admin account gets a password
    (from RAI_NEW_PASSWORD or a prompt); an existing account keeps its own."""
    from rai.core.accounts import create_tenant

    engine = owner_engine()
    try:
        with engine.connect() as conn:
            known = conn.exec_driver_sql(
                "SELECT 1 FROM rai.users WHERE email = %(e)s", {"e": admin_email}
            ).first()
        password_hash = None if known else new_password_hash()
        with engine.begin() as conn:
            result = create_tenant(
                conn,
                name=name,
                slug=slug,
                admin_email=admin_email,
                admin_name=admin_name,
                password_hash=password_hash,
            )
    except IntegrityError as error:
        errors.print(f"error: could not create the tenant: {error.orig}")
        raise typer.Exit(2) from None
    finally:
        engine.dispose()
    who = "new account" if result.admin_created else "existing account"
    console.print(f"tenant {slug} created: {result.tenant_id}; admin {admin_email} ({who})")


@admin_app.command("set-password")
def set_password_command(
    email: Annotated[str, typer.Option(help="Account email")],
) -> None:
    """Set a user's password (from RAI_NEW_PASSWORD or a prompt) and end their sessions."""
    from rai.core.accounts import set_password

    password_hash = new_password_hash()
    engine = owner_engine()
    try:
        with engine.begin() as conn:
            found = set_password(conn, email=email, password_hash=password_hash)
    finally:
        engine.dispose()
    if not found:
        errors.print(f"error: no account for {email}")
        raise typer.Exit(1)
    console.print(f"password set for {email}; existing sessions ended")


def set_api_password_command() -> None:
    """Set the password of the rai_api login role (from RAI_API_DB_PASSWORD or a prompt)."""
    from rai.core.accounts import set_api_role_password

    password = read_password(API_DB_PASSWORD_ENV, "rai_api database password")
    engine = owner_engine()
    try:
        with engine.begin() as conn:
            set_api_role_password(conn, password)
    finally:
        engine.dispose()
    console.print("rai_api password set; point RAI_API_DATABASE_URL at it")


def serve_command(
    host: Annotated[str, typer.Option(help="Address to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8000,
) -> None:
    """Run the API (needs RAI_API_DATABASE_URL and RAI_JWT_SECRET)."""
    import uvicorn

    from rai.api.app import create_app
    from rai.api.settings import Settings, SettingsError

    try:
        application = create_app(Settings.from_env())
    except SettingsError as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from None
    uvicorn.run(application, host=host, port=port, proxy_headers=False, server_header=False)
