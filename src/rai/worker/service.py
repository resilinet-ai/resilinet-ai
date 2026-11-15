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
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from sqlalchemy import Engine, create_engine, text

from rai.connectors import ConnectorError, parse_config
from rai.core.db import login_role_problem, sqlalchemy_url
from rai.core.runs import TERMINAL
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

    def check(self) -> None:
        with self.engine.connect() as conn:
            problem = login_role_problem(conn, "rai_worker")
        if problem is not None:
            raise WorkerSetupError(
                f"the worker must {problem}" if problem.startswith("log in") else problem
            )

    def renew(self, job: queue.Job) -> bool:
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
        self.sweep()
        job = queue.claim(self.engine, self.config.name, self.config.lease)
        if job is None:
            return False
        log.info("run %s: attempt %s of %s", job.run_id, job.attempt, job.max_attempts)
        try:
            self.executor.execute(job)
        except LeaseLost:
            log.warning("run %s: another worker took it over", job.run_id)
            return True
        except Exception as error:  # a platform fault: retry later, then give up
            log.exception("run %s failed on attempt %s", job.run_id, job.attempt)
            queue.release(self.engine, job, self.config.name, f"{type(error).__name__}: {error}")
            return True
        queue.finish(self.engine, job, self.config.name)
        return True

    def serve(self, stop: threading.Event, poll_seconds: float = 2.0) -> None:
        while not stop.is_set():
            if not self.run_once():
                stop.wait(poll_seconds)


def build(settings: WorkerSettings) -> Worker:
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=2)
    worker = Worker(engine, settings.config)
    worker.check()
    return worker
