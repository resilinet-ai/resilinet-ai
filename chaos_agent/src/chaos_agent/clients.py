"""The agent's two backends, over plain HTTP (standard library only).

* **Toxiproxy** admin API (``RAI_AGENT_TOXIPROXY_URL``), reachable only on the
  private ``chaos_net``.
* **Docker** API through the socket proxy (``RAI_AGENT_DOCKER_URL``), which allows
  only listing and inspecting containers, container stop, start, restart, pause and
  unpause, and network connect and disconnect (threat E-01). Anything else the agent
  sent would be refused there too.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

DOCKER_API = "/v1.41"  # Docker Engine 20.10 and later


class BackendError(RuntimeError):
    """A backend call failed."""


def _call(base: str, method: str, path: str, body: Any = None, timeout: float = 15) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed http(s) base URLs from configuration
        base.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "rai-chaos-agent"},
    )  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise BackendError(f"{method} {path}: HTTP {error.code} {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise BackendError(f"{method} {path}: {getattr(error, 'reason', error)}") from None
    return json.loads(raw) if raw.strip() else None


class Toxiproxy(Protocol):
    def proxy(self, name: str) -> dict[str, Any] | None: ...
    def add_toxic(self, proxy: str, toxic: dict[str, Any]) -> None: ...
    def remove_toxic(self, proxy: str, name: str) -> None: ...
    def set_enabled(self, proxy: str, enabled: bool) -> None: ...


class Docker(Protocol):
    def find(self, service: str, project: str) -> list[dict[str, Any]]: ...
    def project_of(self, container: str) -> str | None: ...
    def inspect(self, container_id: str) -> dict[str, Any]: ...
    def action(self, container_id: str, verb: str) -> None: ...
    def disconnect(self, network: str, container_id: str) -> None: ...
    def connect(self, network: str, container_id: str, aliases: list[str]) -> None: ...


class HttpToxiproxy:
    def __init__(self, base_url: str) -> None:
        self.base = base_url

    def proxy(self, name: str) -> dict[str, Any] | None:
        try:
            found: dict[str, Any] = _call(self.base, "GET", f"/proxies/{name}")
        except BackendError as error:
            if "HTTP 404" in str(error):
                return None
            raise
        return found

    def add_toxic(self, proxy: str, toxic: dict[str, Any]) -> None:
        _call(self.base, "POST", f"/proxies/{proxy}/toxics", toxic)

    def remove_toxic(self, proxy: str, name: str) -> None:
        try:
            _call(self.base, "DELETE", f"/proxies/{proxy}/toxics/{name}")
        except BackendError as error:
            if "HTTP 404" not in str(error):  # already gone: reverted
                raise

    def set_enabled(self, proxy: str, enabled: bool) -> None:
        _call(self.base, "POST", f"/proxies/{proxy}", {"enabled": enabled})


class HttpDocker:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + DOCKER_API

    def find(self, service: str, project: str) -> list[dict[str, Any]]:
        """Containers of one Compose service in one project, whatever their state."""
        filters = json.dumps({"label": [f"com.docker.compose.service={service}",
                                        f"com.docker.compose.project={project}"]})  # fmt: skip
        found: list[dict[str, Any]] = _call(
            self.base, "GET", f"/containers/json?all=true&filters={urllib.parse.quote(filters)}"
        )
        return found

    def inspect(self, container_id: str) -> dict[str, Any]:
        found: dict[str, Any] = _call(self.base, "GET", f"/containers/{container_id}/json")
        return found

    def project_of(self, container: str) -> str | None:
        """The Compose project of a container (the agent asks about itself)."""
        try:
            labels = self.inspect(container).get("Config", {}).get("Labels") or {}
        except BackendError:
            return None
        project = labels.get("com.docker.compose.project")
        return str(project) if project else None

    def action(self, container_id: str, verb: str) -> None:
        if verb not in ("stop", "start", "restart", "pause", "unpause"):
            raise BackendError(f"{verb} is not an allowed container action")
        try:
            _call(self.base, "POST", f"/containers/{container_id}/{verb}", timeout=60)
        except BackendError as error:
            # 304: already in that state; 409 "is not paused": already unpaused. Reverts
            # must be idempotent: the agent and the watchdog may both try one (B-25).
            text = str(error)
            if "HTTP 304" not in text and not (verb == "unpause" and "is not paused" in text):
                raise

    def disconnect(self, network: str, container_id: str) -> None:
        _call(self.base, "POST", f"/networks/{network}/disconnect",
              {"Container": container_id, "Force": True})  # fmt: skip

    def connect(self, network: str, container_id: str, aliases: list[str]) -> None:
        try:
            _call(self.base, "POST", f"/networks/{network}/connect",
                  {"Container": container_id, "EndpointConfig": {"Aliases": aliases}})  # fmt: skip
        except BackendError as error:
            if "already exists" not in str(error):  # already connected: reverted (B-25)
                raise
