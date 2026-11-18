"""``rai admin`` (operator, schema-owner credentials) and ``rai serve`` (B-16).

Passwords come from an environment variable (for automation) or a hidden prompt,
never from a command-line argument, which would end up in shell history.
"""

from __future__ import annotations

import os
from pathlib import Path
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
WORKER_DB_PASSWORD_ENV = "RAI_WORKER_DB_PASSWORD"  # noqa: S105 - a variable name


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


def _set_role_password(role: str, env: str, url_env: str) -> None:
    from rai.core.accounts import set_role_password

    password = read_password(env, f"{role} database password")
    engine = owner_engine()
    try:
        with engine.begin() as conn:
            set_role_password(conn, role, password)
    finally:
        engine.dispose()
    console.print(f"{role} password set; point {url_env} at it")


def set_api_password_command() -> None:
    """Set the password of the rai_api login role (from RAI_API_DB_PASSWORD or a prompt)."""
    _set_role_password("rai_api", API_DB_PASSWORD_ENV, "RAI_API_DATABASE_URL")


def set_worker_password_command() -> None:
    """Set the password of the rai_worker login role (RAI_WORKER_DB_PASSWORD or a prompt)."""
    _set_role_password("rai_worker", WORKER_DB_PASSWORD_ENV, "RAI_WORKER_DATABASE_URL")


@admin_app.command("chaos-keygen")
def chaos_keygen_command(
    out: Annotated[
        Path, typer.Option(help="Where to write the worker's private key (PEM); must not exist")
    ],
) -> None:
    """Make the worker's Ed25519 key for signing fault requests (B-24).

    Writes the private key to OUT (owner-only permissions) for RAI_WORKER_SIGNING_KEY
    (file:OUT), and prints the public key alone on stdout for the chaos agent's
    RAI_AGENT_WORKER_KEYS.
    """
    from rai.worker.chaos import Signer

    if out.exists():
        errors.print(f"error: {out} exists; a key is never overwritten")
        raise typer.Exit(2)
    out.parent.mkdir(parents=True, exist_ok=True)
    signer = Signer.generate()
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(signer.pem())
    errors.print(f"private key written to {out} (key id {signer.key_id}); "
                 f"set RAI_WORKER_SIGNING_KEY=file:{out.resolve().as_posix()}")  # fmt: skip
    print(signer.public_key)  # stdout carries only the public key, for scripts


@admin_app.command("evidence-keygen")
def evidence_keygen_command(
    out: Annotated[
        Path, typer.Option(help="Where to write the API's private key (PEM); must not exist")
    ],
) -> None:
    """Make the API's Ed25519 key for signing evidence bundles (B-35: FR-13.2).

    Writes the private key to OUT (owner-only permissions) for RAI_EVIDENCE_SIGNING_KEY
    (file:OUT), and prints the public key alone on stdout: publish it where verifiers
    can trust it, for ``rai verify --key``.
    """
    from rai.core.evidence import EvidenceSigner

    if out.exists():
        errors.print(f"error: {out} exists; a key is never overwritten")
        raise typer.Exit(2)
    out.parent.mkdir(parents=True, exist_ok=True)
    signer = EvidenceSigner.generate()
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(signer.pem())
    errors.print(f"private key written to {out} (key id {signer.key_id}); "
                 f"set RAI_EVIDENCE_SIGNING_KEY=file:{out.resolve().as_posix()}")  # fmt: skip
    print(signer.public_key)  # stdout carries only the public key, for scripts


def worker_command(
    once: Annotated[bool, typer.Option(help="Process at most one job, then exit")] = False,
    poll: Annotated[float, typer.Option(help="Seconds between polls when idle")] = 2.0,
) -> None:
    """Run the worker: claim queued runs and execute them (needs RAI_WORKER_DATABASE_URL)."""
    import logging
    import signal
    import threading

    from rai.worker.service import WorkerSettings, WorkerSetupError, build

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        worker = build(WorkerSettings.from_env())
    except WorkerSetupError as error:
        errors.print(f"error: {error}")
        raise typer.Exit(2) from None
    if once:
        processed = worker.run_once()
        console.print("processed one job" if processed else "no job waiting")
        return
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    console.print(f"worker {worker.config.name} started; Ctrl+C to stop")
    worker.serve(stop, poll_seconds=poll)


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
