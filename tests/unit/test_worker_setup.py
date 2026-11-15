"""Worker configuration: connector folder and settings (B-18)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rai.core.accounts import set_role_password
from rai.core.runs import step_key
from rai.worker.service import WorkerSettings, WorkerSetupError, load_connectors, worker_name


def write(folder: Path, name: str, body: str) -> None:
    (folder / name).write_text(body, encoding="utf-8")


def test_connectors_are_loaded_by_name_and_validated(tmp_path: Path) -> None:
    assert load_connectors(None) == {}
    write(tmp_path, "a.yaml", "name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: /data\n")
    write(tmp_path, "notes.txt", "ignored")
    assert set(load_connectors(tmp_path)) == {"feed"}
    write(tmp_path, "b.yaml", "name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: /other\n")
    with pytest.raises(WorkerSetupError, match="used twice"):
        load_connectors(tmp_path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("- a list\n", "must be a mapping"),
        ("name: [unclosed\n", "b.yaml"),
        ("name: x\nenvironment: E\ntype: postgres\npassword: hunter2\n", "b.yaml"),
    ],
)
def test_bad_connector_configs_stop_the_worker(tmp_path: Path, body: str, message: str) -> None:
    write(tmp_path, "b.yaml", body)
    with pytest.raises(WorkerSetupError, match=message):
        load_connectors(tmp_path)
    with pytest.raises(WorkerSetupError, match="not a folder"):
        load_connectors(tmp_path / "b.yaml")


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "RAI_WORKER_DATABASE_URL",
        "RAI_WORKER_CONNECTORS",
        "RAI_WORKER_STATE",
        "RAI_WORKER_SIGNING_KEY",
        "RAI_WORKER_CHAOS_AGENT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(WorkerSetupError, match="RAI_WORKER_DATABASE_URL"):
        WorkerSettings.from_env()
    monkeypatch.setenv("RAI_WORKER_DATABASE_URL", "postgresql://rai_worker@localhost/rai")
    settings = WorkerSettings.from_env()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert (settings.config.connectors, settings.config.state_dir) == ({}, Path(".rai/worker"))
    monkeypatch.setenv("RAI_WORKER_CONNECTORS", str(tmp_path))
    monkeypatch.setenv("RAI_WORKER_STATE", str(tmp_path / "state"))
    assert WorkerSettings.from_env().config.state_dir == tmp_path / "state"
    assert WorkerSettings.from_env().config.chaos is None  # no faults without a key (B-24)
    assert worker_name() != worker_name()


def test_a_half_configured_chaos_client_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rai.worker.chaos import Signer

    monkeypatch.setenv("RAI_WORKER_DATABASE_URL", "postgresql://rai_worker@localhost/rai")
    monkeypatch.setenv("RAI_WORKER_CHAOS_AGENT_URL", "http://rai-chaos-agent:8090")
    monkeypatch.delenv("RAI_WORKER_SIGNING_KEY", raising=False)
    with pytest.raises(WorkerSetupError, match="RAI_WORKER_SIGNING_KEY"):
        WorkerSettings.from_env()
    key = tmp_path / "worker.pem"
    key.write_bytes(Signer.generate().pem())
    monkeypatch.setenv("RAI_WORKER_SIGNING_KEY", f"file:{key.as_posix()}")
    chaos = WorkerSettings.from_env().config.chaos
    assert chaos is not None
    assert chaos.agent.url == "http://rai-chaos-agent:8090"


def test_only_service_roles_get_passwords_this_way() -> None:
    with pytest.raises(ValueError, match="rai_api, rai_worker"):
        set_role_password(None, "postgres", "x")  # type: ignore[arg-type]
    assert step_key(3) == "item-3"
