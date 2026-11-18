"""The TLS edge in compose.yaml and the Caddyfile (B-36: NFR-SEC-2; threat I-06).

These read the files as configuration; ``make tls-e2e`` and the CI job "TLS edge (Caddy)"
run the stack itself.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from rai.cli.main import app

ROOT = Path(__file__).resolve().parents[2]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
SERVICES: dict[str, Any] = COMPOSE["services"]
CADDYFILE = "\n".join(  # without comments
    line
    for line in (ROOT / "deploy" / "caddy" / "Caddyfile").read_text("utf-8").splitlines()
    if not line.lstrip().startswith("#")
)


def networks_of(service: dict[str, Any]) -> set[str]:
    found = service.get("networks", [])
    return set(found) if isinstance(found, list) else set(found.keys())


def test_every_published_port_is_host_only_by_default() -> None:
    for name, service in SERVICES.items():
        for port in service.get("ports", []):
            text = str(port)
            assert text.startswith(("127.0.0.1:", "${RAI_HTTPS_BIND:-127.0.0.1}:")), (name, port)


def test_caddy_is_the_only_way_to_the_api() -> None:
    api, caddy = SERVICES["rai-api"], SERVICES["caddy"]
    assert "ports" not in api  # no host port: only through Caddy
    assert networks_of(api) == {"control_net", "edge_net"}
    edge = COMPOSE["networks"]["edge_net"]
    assert edge["internal"] is True
    on_edge = {n for n, s in SERVICES.items() if "edge_net" in networks_of(s)}
    assert on_edge == {"rai-api", "caddy"}
    assert "sandbox_net" not in networks_of(api)  # TB1: the API never joins sandbox_net
    [port] = caddy["ports"]
    assert str(port).endswith(":443")  # one port, the TLS one
    # rai-api trusts forwarded headers from Caddy's fixed address, and only from it.
    caddy_ip = caddy["networks"]["edge_net"]["ipv4_address"]
    command = api["command"]
    assert command[command.index("--forwarded-allow-ips") + 1] == caddy_ip
    subnet = edge["ipam"]["config"][0]["subnet"]
    assert caddy_ip.rsplit(".", 1)[0] == subnet.rsplit(".", 1)[0]


def test_the_edge_containers_are_locked_down() -> None:
    for name in ("rai-api", "caddy"):
        service = SERVICES[name]
        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
    assert SERVICES["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+", SERVICES["caddy"]["image"])  # a pinned tag


def test_caddy_serves_https_only_with_security_headers() -> None:
    assert "admin off" in CADDYFILE
    assert "auto_https disable_redirects" in CADDYFILE  # no plain-HTTP listener at all
    assert "http://" not in CADDYFILE
    assert "tls {$RAI_TLS:internal}" in CADDYFILE
    for header in ('Strict-Transport-Security "max-age=31536000"',
                   'X-Content-Type-Options "nosniff"', "-Server"):  # fmt: skip
        assert header in CADDYFILE
    assert "trusted_proxies" not in CADDYFILE  # clients cannot supply X-Forwarded-For
    assert "reverse_proxy rai-api:8000" in CADDYFILE


def test_the_api_image_installs_exactly_the_locked_versions() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {p["name"]: p["version"] for p in lock["package"]}
    project = next(p for p in lock["package"] if p["name"] == "resilinet-ai")
    text = (ROOT / "deploy" / "rai" / "requirements.txt").read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([a-z0-9_.-]+)==([^\s;]+)", text, flags=re.MULTILINE))
    assert {d["name"] for d in project["dependencies"]} <= set(pinned)
    for name, version in pinned.items():
        assert locked.get(name) == version, f"{name}: run the uv export in requirements.txt"
    assert "--hash=sha256:" in text
    dockerfile = (ROOT / "deploy" / "rai" / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile
    assert "USER 10001" in dockerfile


def test_serve_trusts_forwarded_headers_only_from_named_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda application, **options: calls.append(options))
    monkeypatch.setattr("rai.api.settings.Settings.from_env", classmethod(lambda cls: object()))
    monkeypatch.setattr("rai.api.app.create_app", lambda settings: "app")
    monkeypatch.delenv("RAI_FORWARDED_ALLOW_IPS", raising=False)
    runner = CliRunner()
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert (calls[-1]["proxy_headers"], calls[-1]["forwarded_allow_ips"]) == (False, None)
    result = runner.invoke(app, ["serve", "--forwarded-allow-ips", "172.31.236.2, 10.0.0.0/8"])
    assert result.exit_code == 0, result.output
    assert calls[-1]["proxy_headers"] is True
    assert calls[-1]["forwarded_allow_ips"] == ["172.31.236.2", "10.0.0.0/8"]
    refused = runner.invoke(app, ["serve", "--forwarded-allow-ips", "*"])
    assert refused.exit_code == 2
    assert len(calls) == 2
