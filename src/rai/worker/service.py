"""The worker process: claim a job, execute its run, repeat (B-18: FR-06.2).

Settings from the environment:

* ``RAI_WORKER_DATABASE_URL``: rai-db, logging in as ``rai_worker``. The worker
  refuses to start as anything more powerful (threat I-02).
* ``RAI_WORKER_CONNECTORS``: a folder of connector configs (``*.yaml``, the B-13
  format) that contract-rule steps may read through. Optional; without it every
  contract-rule run is refused at preflight.
* ``RAI_WORKER_STATE``: where the cross-batch key history lives, one folder per
  tenant (default ``.rai/worker``).
* ``RAI_WORKER_SIGNING_KEY`` and ``RAI_WORKER_CHAOS_AGENT_URL`` (B-24): a secret
  reference to the worker's Ed25519 key (``rai admin chaos-keygen``) and the chaos
  agent's URL. Both or neither; without them the worker requests no faults.

Health (NFR-OPS-1): with ``rai worker --health-port``, ``GET /healthz`` answers 200
while the worker has shown a sign of life (a poll, a claimed job or a lease renewal)
within one lease, else 503: a worker silent for a whole lease has lost its job to
another anyway. ``GET /readyz`` also checks the database. Each run is a span.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import Engine, create_engine, text

from rai.connectors import ConnectorError, parse_config
from rai.core.db import login_role_problem, sqlalchemy_url
from rai.core.runs import TERMINAL
from rai.core.telemetry import span
from rai.worker import queue
from rai.worker.chaos import ChaosClient, ChaosSetupError
from rai.worker.chaos import from_env as chaos_from_env
from rai.worker.executor import Executor, LeaseLost, WorkerConfig, move, tenant_transaction

log = logging.getLogger("rai.worker")

DATABASE_URL_ENV = "RAI_WORKER_DATABASE_URL"
CONNECTORS_ENV = "RAI_WORKER_CONNECTORS"
STATE_ENV = "RAI_WORKER_STATE"


class WorkerSetupError(RuntimeError):
    """The worker cannot start with this configuration."""


def worker_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:6]}"


def load_connectors(folder: Path | None) -> dict[str, dict[str, Any]]:
    """Operator-provided connector configs by name; each is validated now."""
    if folder is None:
        return {}
    if not folder.is_dir():
        raise WorkerSetupError(f"{CONNECTORS_ENV}: {folder} is not a folder")
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(folder.glob("*.yaml")):
        try:
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise WorkerSetupError(f"{path.name}: a connector config must be a mapping")
            name = parse_config(config).name
        except (yaml.YAMLError, ConnectorError) as error:
            raise WorkerSetupError(f"{path.name}: {error}") from None
        if name in found:
            raise WorkerSetupError(f"{path.name}: connector name {name!r} is used twice")
        found[name] = config
    return found


def _chaos_client() -> ChaosClient | None:
    try:
        return chaos_from_env(dict(os.environ))
    except ChaosSetupError as error:
        raise WorkerSetupError(str(error)) from None


@dataclass(frozen=True)
class WorkerSettings:
    database_url: str
    config: WorkerConfig

    @classmethod
    def from_env(cls) -> WorkerSettings:
        url = os.environ.get(DATABASE_URL_ENV)
        if not url:
            raise WorkerSetupError(f"set {DATABASE_URL_ENV} to the rai-db URL for rai_worker")
        connectors_dir = os.environ.get(CONNECTORS_ENV)
        config = WorkerConfig(
            name=worker_name(),
            state_dir=Path(os.environ.get(STATE_ENV, ".rai/worker")),
            connectors=load_connectors(Path(connectors_dir) if connectors_dir else None),
            chaos=_chaos_client(),
        )
        return cls(sqlalchemy_url(url), config)


class Worker:
    def __init__(self, engine: Engine, config: WorkerConfig, executor: Executor | None = None):
        self.engine, self.config = engine, config
        self.executor = executor or Executor(engine, config, renew=self.renew)
        self.heartbeat = time.monotonic()  # the last sign of life, for /healthz

    def check(self) -> None:
        with self.engine.connect() as conn:
            problem = login_role_problem(conn, "rai_worker")
        if problem is not None:
            raise WorkerSetupError(
                f"the worker must {problem}" if problem.startswith("log in") else problem
            )

    def renew(self, job: queue.Job) -> bool:
        self.heartbeat = time.monotonic()
        return queue.renew(self.engine, job, self.config.name, self.config.lease)

    def sweep(self) -> int:
        """End runs whose job used every attempt: state ``error`` with the last error."""
        closed = 0
        for job in queue.exhausted(self.engine):
            as_job = queue.Job(job.job_id, job.tenant_id, job.run_id, job.attempts, job.attempts)
            with tenant_transaction(self.engine, as_job, self.config.name) as conn:
                state = conn.execute(
                    text("SELECT state::text FROM rai.runs WHERE run_id = :r"),
                    {"r": job.run_id},
                ).scalar_one()
            if state not in TERMINAL:
                reason = f"the worker failed {job.attempts} times; last error: {job.last_error}"
                move(self.engine, as_job, self.config.name, "error", reason=reason[:2000])
            queue.close_exhausted(self.engine, job.job_id)
            closed += 1
        return closed

    def run_once(self) -> bool:
        """Claim and execute one job; True if there was one."""
        self.heartbeat = time.monotonic()
        self.sweep()
        job = queue.claim(self.engine, self.config.name, self.config.lease)
        if job is None:
            return False
        ids: dict[str, Any] = {
            "run_id": str(job.run_id), "tenant_id": str(job.tenant_id), "attempt": job.attempt,
        }  # fmt: skip
        with span("worker.run", worker=self.config.name, **ids) as current:
            log.info("run %s: attempt %s of %s", job.run_id, job.attempt, job.max_attempts,
                     extra={"fields": ids})  # fmt: skip
            try:
                self.executor.execute(job)
            except LeaseLost:
                current.attributes["outcome"] = "lease_lost"
                log.warning("run %s: another worker took it over", job.run_id,
                            extra={"fields": ids})  # fmt: skip
                return True
            except Exception as error:  # a platform fault: retry later, then give up
                current.status, current.attributes["outcome"] = "error", "released"
                log.exception("run %s failed on attempt %s", job.run_id, job.attempt,
                              extra={"fields": ids})  # fmt: skip
                queue.release(
                    self.engine, job, self.config.name, f"{type(error).__name__}: {error}"
                )
                return True
            queue.finish(self.engine, job, self.config.name)
            current.attributes["outcome"] = "finished"
            return True

    def alive(self) -> bool:
        """A sign of life within one lease (see the module docstring)."""
        return time.monotonic() - self.heartbeat < self.config.lease.total_seconds()

    def ready(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # any database failure means not ready
            return False
        return True

    def serve(self, stop: threading.Event, poll_seconds: float = 2.0) -> None:
        while not stop.is_set():
            if not self.run_once():
                stop.wait(poll_seconds)


def serve_health(worker: Worker, host: str, port: int) -> ThreadingHTTPServer:
    """``/healthz`` and ``/readyz`` on a background thread; returns the server."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                ok = worker.alive()
            elif self.path == "/readyz":
                ok = worker.alive() and worker.ready()
            else:
                self.send_error(404)
                return
            body = b'{"status":"ok"}' if ok else b'{"status":"unavailable"}'
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # probes every few seconds would drown the log

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
    return server


def build(settings: WorkerSettings) -> Worker:
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=2)
    worker = Worker(engine, settings.config)
    worker.check()
    return worker
