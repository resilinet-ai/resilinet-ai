"""The agent's local HTTP interface and its command line (B-23).

    rai-chaos-agent serve           # listen (RAI_AGENT_LISTEN, default 0.0.0.0:8090)
    rai-chaos-agent status          # the open faults in the state file
    rai-chaos-agent kill --all      # revert every open fault from the state file

HTTP:

* ``POST /faults``: inject one fault; ``201`` with the fault and its onset (t0).
  **Refused (403) unless the agent was started with ``RAI_AGENT_UNSIGNED_DEV=1``**:
  signed requests from the worker arrive with B-24, and until then a deployed agent
  injects nothing.
* ``DELETE /faults/{id}``: revert one fault. ``POST /kill``: revert all. Reverting
  is always allowed: it only ever makes the sandbox healthier.
* ``GET /faults``: open faults. ``GET /healthz``.

Configuration (environment): ``RAI_AGENT_TOXIPROXY_URL``, ``RAI_AGENT_DOCKER_URL``
(the socket proxy), ``RAI_AGENT_STATE`` (state file path).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from chaos_agent.agent import Agent
from chaos_agent.clients import BackendError, HttpDocker, HttpToxiproxy
from chaos_agent.faults import FaultError, FaultRequest, StateFile

DEV_FLAG = "RAI_AGENT_UNSIGNED_DEV"
MAX_BODY = 16 * 1024
EXPIRY_INTERVAL = 1.0


def build(environ: dict[str, str] | None = None) -> Agent:
    env = os.environ if environ is None else environ
    docker = HttpDocker(env.get("RAI_AGENT_DOCKER_URL", "http://socket-proxy:2375"))
    # The agent acts only inside its own Compose project: it reads the project from its
    # own container's labels (its hostname is its container id).
    project = env.get("RAI_AGENT_COMPOSE_PROJECT") or docker.project_of(socket.gethostname())
    return Agent(
        StateFile(Path(env.get("RAI_AGENT_STATE", "/var/lib/rai-chaos-agent/state.json"))),
        HttpToxiproxy(env.get("RAI_AGENT_TOXIPROXY_URL", "http://toxiproxy:8474")),
        docker,
        project,
    )


def handler_for(agent: Agent, *, unsigned_allowed: bool) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "rai-chaos-agent"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            logging.getLogger("rai.chaos_agent.http").info(format, *args)

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _problem(self, status: int, message: str) -> None:
            self._send(status, {"error": message})

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"status": "ok"})
            elif self.path == "/faults":
                self._send(200, {"faults": [f.to_json() for f in agent.open_faults()]})
            else:
                self._problem(404, "not found")

        def do_POST(self) -> None:
            if self.path == "/kill":
                reverted = agent.revert_all()
                self._send(200, {"reverted": [f.fault_id for f in reverted]})
                return
            if self.path != "/faults":
                self._problem(404, "not found")
                return
            if not unsigned_allowed:
                self._problem(HTTPStatus.FORBIDDEN, "unsigned fault requests are refused; "
                              "signed requests from the worker arrive with B-24")  # fmt: skip
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                self._problem(400, "send a JSON body of at most 16 KiB")
                return
            try:
                request = FaultRequest.parse(json.loads(self.rfile.read(length)))
                fault = agent.inject(request)
            except (FaultError, ValueError) as error:
                self._problem(422, str(error))
            except BackendError as error:
                self._problem(502, f"the fault could not be applied and was undone: {error}")
            else:
                self._send(201, fault.to_json())

        def do_DELETE(self) -> None:
            prefix = "/faults/"
            if not self.path.startswith(prefix):
                self._problem(404, "not found")
                return
            try:
                fault = agent.revert(self.path.removeprefix(prefix))
            except KeyError:
                self._problem(404, "no such fault")
            except BackendError as error:
                self._problem(502, f"revert failed: {error}")
            else:
                self._send(200, fault.to_json())

    return Handler


def expiry_loop(agent: Agent, stop: threading.Event) -> None:
    while not stop.wait(EXPIRY_INTERVAL):
        try:
            agent.revert_expired()
        except Exception:  # keep the loop alive whatever one revert does
            logging.getLogger("rai.chaos_agent").exception("expiry check failed")


def serve(agent: Agent, host: str, port: int, *, unsigned_allowed: bool) -> None:
    left = agent.revert_all()  # faults from before a crash are undone first
    log = logging.getLogger("rai.chaos_agent")
    if left:
        log.warning("reverted %d fault(s) left open by an earlier run", len(left))
    stop = threading.Event()
    threading.Thread(target=expiry_loop, args=(agent, stop), daemon=True).start()
    server = ThreadingHTTPServer(
        (host, port), handler_for(agent, unsigned_allowed=unsigned_allowed)
    )
    log.info("Compose project: %s", agent.project or "UNKNOWN (container faults refused)")
    log.info("listening on %s:%d (unsigned fault requests %s)", host, port,
             "ALLOWED: development only" if unsigned_allowed else "refused")  # fmt: skip
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rai-chaos-agent", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the agent")
    commands.add_parser("status", help="list open faults")
    kill = commands.add_parser("kill", help="revert faults from the state file")
    kill.add_argument("--all", action="store_true", required=True, help="every open fault")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    agent = build()
    if args.command == "serve":
        host, _, port = os.environ.get("RAI_AGENT_LISTEN", "0.0.0.0:8090").rpartition(":")
        serve(agent, host, int(port), unsigned_allowed=os.environ.get(DEV_FLAG) == "1")
        return 0
    if args.command == "status":
        sys.stdout.write(json.dumps([f.to_json() for f in agent.open_faults()], indent=2) + "\n")
        return 0
    reverted = agent.revert_all()
    sys.stdout.write(f"reverted {len(reverted)} fault(s)\n")
    return 1 if agent.open_faults() else 0


if __name__ == "__main__":
    raise SystemExit(main())
