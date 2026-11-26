"""A live RAI for the journey tests (B-46): the API and a worker as real processes, over
real HTTP, beside SandboxBank and the chaos agent started by Docker Compose.

The tests drive RAI only as its users do: the HTTP API, the ``rai`` CLI (``rai run``,
``rai verify``, ``rai audit verify``) and the operator commands an admin would type
(``rai db upgrade``, ``rai admin ...``). Nothing is imported from RAI to change state.

Environment (the nightly job "Journeys J1-J5" sets it; ``make journeys`` too):

* ``RAI_E2E_DATABASE_URL``: rai-db as its owner (compose's ``rai-db``, port 55431).
  Without it every journey test is skipped.
* ``RAI_E2E_WORKER_KEY``: the worker's Ed25519 key (``rai admin chaos-keygen``) whose
  public key the chaos agent trusts (``RAI_AGENT_WORKER_KEYS``).
* ``LEDGER_DB_PASSWORD`` and ``ANALYTICS_DB_PASSWORD``: SandboxBank's databases. The
  worker reads them through secret references (``env:``), never from a config file.

Synthetic data only.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml"
ENVIRONMENT = "SANDBOX-01"  # the agent serves this one (RAI_AGENT_ENVIRONMENT)
AGENT = os.environ.get("RAI_E2E_AGENT_URL", "http://127.0.0.1:18090")
PAYMENTS = os.environ.get("RAI_E2E_PAYMENTS_URL", "http://127.0.0.1:18080")
LEDGER_PORT = int(os.environ.get("RAI_E2E_LEDGER_PORT", "55432"))
ANALYTICS_PORT = int(os.environ.get("RAI_E2E_ANALYTICS_PORT", "55434"))


def tool(name: str) -> str:
    """A console script of this environment (``rai``, ``sandboxbank``)."""
    folder = Path(sys.executable).parent
    for candidate in (folder / name, folder / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(f"{name} is not installed next to {sys.executable}")


def eventually[T](
    check: Callable[[], T | None], what: str, within: float = 60, every: float = 0.5
) -> T:
    deadline = time.monotonic() + within
    while True:
        found = check()
        if found:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {within:.0f} s waiting for {what}")
        time.sleep(every)


@dataclass
class Cli:
    """Runs ``rai`` and ``sandboxbank`` as an operator or a CI job would."""

    env: dict[str, str]
    logs: Path

    def __call__(
        self, *args: str, check: bool = True, **env: str
    ) -> subprocess.CompletedProcess[str]:
        program = tool(args[0])
        done = subprocess.run(  # noqa: S603 - our own tools, fixed arguments
            [program, *args[1:]],
            cwd=ROOT,
            env=self.env | env,
            text=True,
            capture_output=True,
            check=False,
            timeout=900,
        )
        if check and done.returncode:
            raise AssertionError(
                f"{' '.join(args)} exited {done.returncode}:\n{done.stdout}\n{done.stderr}"
            )
        return done

    @contextmanager
    def background(self, name: str, *args: str, **env: str) -> Iterator[subprocess.Popen[bytes]]:
        log = (self.logs / f"{name}.log").open("ab")
        process = subprocess.Popen(  # noqa: S603 - our own tools, fixed arguments
            [tool(args[0]), *args[1:]],
            cwd=ROOT,
            env=self.env | env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            log.close()


@dataclass
class Person:
    """A signed-in member: an HTTP client carrying their token and tenant."""

    email: str
    role: str
    http: httpx.Client
    user_id: str = ""

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        expect: int | tuple[int, ...] = (200, 201, 202, 204),
    ) -> Any:
        response = self.http.request(method, path, json=body)
        wanted = (expect,) if isinstance(expect, int) else expect
        assert response.status_code in wanted, (
            f"{method} {path}: {response.status_code} {response.text}"
        )
        if not response.content or response.headers.get("content-type", "").startswith(
            "application/zip"
        ):
            return response
        return response.json()

    def get(self, path: str, expect: int | tuple[int, ...] = 200) -> Any:
        return self.call("GET", path, expect=expect)

    def post(self, path: str, body: Any = None, expect: int | tuple[int, ...] = (201, 202)) -> Any:
        return self.call("POST", path, body, expect=expect)

    def wait_for_run(self, run_id: str, within: float = 600) -> dict[str, Any]:
        finished = {"passed", "failed", "refused", "aborted", "error"}

        def done() -> dict[str, Any] | None:
            run: dict[str, Any] = self.get(f"/v1/assurance/runs/{run_id}")
            return run if run["state"] in finished else None

        return eventually(done, f"run {run_id} to finish", within=within, every=1)


@dataclass
class Stack:
    """rai-db (migrated, with its login roles), a tenant, the API and a worker."""

    owner_url: str
    work: Path
    cli: Cli
    api_url: str
    tenant_id: str
    admin_email: str
    password: str
    evidence_public_key: str
    sandbox: Path  # SandboxBank's generated dataset, loaded into ledger-db
    feed: Path  # the CSV slice the worker's "feed" connector reads
    connectors: Path
    people: dict[str, Person] = field(default_factory=dict)

    def sign_in(self, email: str, role: str, tenant_id: str | None = None) -> Person:
        http = httpx.Client(base_url=self.api_url, timeout=60)
        response = http.post("/v1/auth/login", json={"email": email, "password": self.password})
        assert response.status_code == 200, response.text
        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        http.headers["X-RAI-Tenant"] = tenant_id or self.tenant_id
        person = Person(email, role, http)
        person.user_id = person.get("/v1/me")["user_id"]
        return person

    def activate(self, email: str) -> None:
        """The operator sets an invited member's first password (``rai admin set-password``)."""
        self.cli(
            "rai",
            "admin",
            "set-password",
            "--email",
            email,
            RAI_DATABASE_URL=self.owner_url,
            RAI_NEW_PASSWORD=self.password,
        )

    def new_tenant(self, admin_email: str) -> str:
        """Another organisation on RAI, made by the operator (``rai admin create-tenant``)."""
        return tenant(self.cli, self.owner_url, self.password, admin_email)

    def audit(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """The tenant's audit chain, read as the owner (an auditor's view of the database)."""
        from sqlalchemy import create_engine, text

        from rai.core.db import sqlalchemy_url

        engine = create_engine(sqlalchemy_url(self.owner_url))
        try:
            with engine.connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT seq, actor_type::text, actor_id, action, subject_type, "
                            "subject_id, payload FROM rai.audit_chain WHERE tenant_id = :t "
                            "ORDER BY seq"
                        ),
                        {"t": tenant_id or self.tenant_id},
                    )
                    .mappings()
                    .all()
                )
        finally:
            engine.dispose()
        return [dict(r) for r in rows]


def _role_url(owner_url: str, role: str, password: str) -> str:
    url = make_url(owner_url).set(username=role, password=password)
    return url.render_as_string(hide_password=False)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def connectors(folder: Path, feed: Path) -> None:
    """The worker's connectors (FR-02.3): files, and SandboxBank's two databases through
    secret references. No credential is ever written here."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "feed.yaml").write_text(
        f"name: feed\nenvironment: {ENVIRONMENT}\ntype: csv\nroot: {feed.as_posix()}\n", "utf-8"
    )
    for name, port, database, schema in (
        ("ledger", LEDGER_PORT, "ledger", "public"),
        ("warehouse", ANALYTICS_PORT, "analytics", "warehouse"),
        ("features", ANALYTICS_PORT, "analytics", "features"),  # P3 (the chaos dry run)
    ):
        user = "ledger" if name == "ledger" else "analytics"  # warehouse and features
        secret = "LEDGER_DB_PASSWORD" if name == "ledger" else "ANALYTICS_DB_PASSWORD"
        (folder / f"{name}.yaml").write_text(
            f"name: {name}\nenvironment: {ENVIRONMENT}\ntype: postgres\nhost: 127.0.0.1\n"
            f"port: {port}\ndatabase: {database}\nuser: {user}\nsecret_ref: env:{secret}\n"
            f"sslmode: disable\nschema_name: {schema}\nstatement_timeout_seconds: 30\n",
            "utf-8",
        )


def sandboxbank(cli: Cli, out: Path) -> None:
    """SandboxBank with a history: reference data and one batch posted through the API,
    so the ledger and (after the ETL) the warehouse hold rows before any fault."""
    cli(
        "sandboxbank",
        "generate",
        "--seed",
        "46",
        "--out",
        str(out),
        "--accounts",
        "200",
        "--merchants",
        "40",
        "--rows",
        "1000",
        "--batches",
        "2",
        "--force",
    )
    dsn = f"postgresql://ledger:{os.environ['LEDGER_DB_PASSWORD']}@127.0.0.1:{LEDGER_PORT}/ledger"
    cli("sandboxbank", "load-reference", "--data", str(out), SANDBOXBANK_LEDGER_DSN=dsn)
    cli("sandboxbank", "post", "--data", str(out), "--batch", "B000000", "--api", PAYMENTS)


@contextmanager
def started(owner_url: str, work: Path) -> Iterator[Stack]:
    from sandboxbank.fixtures import build_slice

    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    api_password, worker_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    base = {k: v for k, v in os.environ.items() if not k.startswith("RAI_")}
    cli = Cli(base | {"PYTHONIOENCODING": "utf-8"}, logs)

    cli("rai", "db", "upgrade", RAI_DATABASE_URL=owner_url)
    cli("rai", "db", "set-api-password", RAI_DATABASE_URL=owner_url,
        RAI_API_DB_PASSWORD=api_password)  # fmt: skip
    cli("rai", "db", "set-worker-password", RAI_DATABASE_URL=owner_url,
        RAI_WORKER_DB_PASSWORD=worker_password)  # fmt: skip
    public_key = cli(
        "rai", "admin", "evidence-keygen", "--out", str(work / "keys" / "evidence.pem")
    ).stdout.strip()

    feed = work / "slice"
    build_slice(
        feed, data_seed=27, injector_seed=28, accounts=200, merchants=40, rows_per_batch=2_000
    )
    sandbox = work / "sandboxbank"
    sandboxbank(cli, sandbox)
    connectors(work / "connectors", feed / "feed")

    port = _free_port()
    api_url = f"http://127.0.0.1:{port}"
    api_env = {
        "RAI_API_DATABASE_URL": _role_url(owner_url, "rai_api", api_password),
        "RAI_JWT_SECRET": secrets.token_urlsafe(48),
        "RAI_EVIDENCE_SIGNING_KEY": f"file:{(work / 'keys' / 'evidence.pem').as_posix()}",
    }
    worker_env = {
        "RAI_WORKER_DATABASE_URL": _role_url(owner_url, "rai_worker", worker_password),
        "RAI_WORKER_CONNECTORS": str(work / "connectors"),
        "RAI_WORKER_STATE": str(work / "worker-state"),
        "RAI_WORKER_SIGNING_KEY": f"file:{Path(os.environ['RAI_E2E_WORKER_KEY']).as_posix()}",
        "RAI_WORKER_CHAOS_AGENT_URL": AGENT,
    }
    with (
        cli.background("rai-api", "rai", "serve", "--port", str(port), **api_env) as api,
        cli.background("rai-worker", "rai", "worker", "--poll", "0.5", **worker_env) as worker,
    ):

        def up() -> bool:
            if api.poll() is not None or worker.poll() is not None:
                raise AssertionError(f"the API or the worker exited; see {logs}")
            try:
                return httpx.get(f"{api_url}/healthz", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        eventually(up, "the API", within=60)
        admin_email = f"admin-{secrets.token_hex(4)}@example.test"
        tenant_id = tenant(cli, owner_url, password, admin_email)
        yield Stack(
            owner_url,
            work,
            cli,
            api_url,
            tenant_id,
            admin_email,
            password,
            public_key,
            sandbox,
            feed,
            work / "connectors",
        )


def tenant(cli: Cli, owner_url: str, password: str, admin_email: str) -> str:
    slug = f"e2e-{secrets.token_hex(4)}"
    made = cli(
        "rai",
        "admin",
        "create-tenant",
        "--slug",
        slug,
        "--name",
        slug,
        "--admin-email",
        admin_email,
        "--admin-name",
        "Journey Admin",
        RAI_DATABASE_URL=owner_url,
        RAI_NEW_PASSWORD=password,
    )
    found = re.search(r"created: ([0-9a-f-]{36})", made.stdout)
    assert found, made.stdout
    return found.group(1)


def docker_image(label: str) -> str:
    """The image ID of the running container that carries ``label`` (B-45)."""
    ids = subprocess.run(  # noqa: S603 - docker, fixed arguments
        ["docker", "ps", "-q", "--filter", f"label={label}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(ids) == 1, f"expected one container labelled {label}, found {len(ids)}"
    image = subprocess.run(  # noqa: S603 - docker, fixed arguments
        ["docker", "inspect", "--format", "{{.Image}}", ids[0]],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return image
