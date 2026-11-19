"""Each fault type, observable on SandboxBank (B-23: FR-07.1 acceptance). Synthetic only.

Every fault request is signed with a worker key the agent trusts (B-24, threat S-03),
exactly as the worker signs it (``rai.worker.chaos.Signer``). The CI job "Chaos
agent" (and ``make chaos-e2e``) runs it like this:

    RAI_AGENT_WORKER_KEYS="$(uv run rai admin chaos-keygen --out /tmp/k/worker.pem)" \\
      ETL_INTERVAL_SECONDS=2 RAI_WATCHDOG_GRACE_SECONDS=3 \\
      docker compose --profile sandbox --profile chaos up -d --build --wait
    RAI_CHAOS_E2E_KEY=/tmp/k/worker.pem uv run python scripts/chaos_e2e.py

For each of the seven fault types it injects one fault through the agent's HTTP
interface, shows its effect on SandboxBank, reverts it, and shows the recovery. It
also checks that the agent refuses unsigned, forged, replayed, expired and
other-environment requests. Then the kill-switch drill (B-25: FR-07.3, threat D-01):
the agent is killed mid-fault and the independent watchdog restores the target; and
``kill --all`` reverts every open fault from the state file alone, in a stack with no
RAI API and no database. Exit code 0 only if every check passed.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from rai.worker.chaos import Signer

AGENT = os.environ.get("RAI_AGENT_URL", "http://127.0.0.1:18090")
PAYMENTS = os.environ.get("SANDBOXBANK_URL", "http://127.0.0.1:18080")
ENVIRONMENT = os.environ.get("RAI_AGENT_ENVIRONMENT", "SANDBOXBANK")
LATENCY_MS = 500
RUN = f"e2e-{uuid.uuid4()}"  # one run per session; every fault gets its own step
_steps = iter(range(1, 1000))


@cache
def signer() -> Signer:
    return Signer.from_pem(Path(os.environ["RAI_CHAOS_E2E_KEY"]).read_bytes())


def fault_request(body: dict[str, Any]) -> dict[str, Any]:
    """What the worker would sign: the fault, its run and step, the environment."""
    return {"duration_seconds": 120, "blast_radius": [body["target"]], "run_id": RUN,
            "step_key": f"step-{next(_steps)}", "environment": ENVIRONMENT,
            "tenant_id": "e2e"} | body  # fmt: skip


def http(url: str, method: str = "GET", body: Any = None, timeout: float = 5) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed local URLs
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, _json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _json(error.read())


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        return raw.decode("utf-8", "replace")[:200]  # e.g. a plain-text 500 while recovering


def expect(condition: bool, problem: str) -> None:
    """An explicit check (asserts vanish under python -O)."""
    if not condition:
        raise AssertionError(problem)


def ready(timeout: float = 3) -> bool:
    try:
        return http(f"{PAYMENTS}/readyz", timeout=timeout)[0] == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def readyz_seconds() -> float:
    start = time.perf_counter()
    if not ready(timeout=10):
        raise AssertionError("payments-api is not ready")
    return time.perf_counter() - start


def eventually(check: Callable[[], bool], what: str, within: float = 60) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise AssertionError(f"timed out waiting until {what}")


def container(service: str) -> str:
    out = subprocess.run(["docker", "compose", "ps", "-aq", service], check=True,  # noqa: S603, S607
                         capture_output=True, text=True).stdout.split()  # fmt: skip
    if len(out) != 1:
        raise AssertionError(f"expected one {service} container, found {len(out)}")
    return out[0]


def inspect(service: str, field: str) -> str:
    return subprocess.run(["docker", "inspect", "-f", field, container(service)],  # noqa: S603, S607
                          check=True, capture_output=True, text=True).stdout.strip()  # fmt: skip


def etl_cycles(since: str) -> int:
    """ETL cycles logged since a moment (one JSON line per cycle, NFR-OPS-1)."""
    logs = subprocess.run(["docker", "logs", "--since", since, container("etl-job")],  # noqa: S603, S607
                          check=True, capture_output=True, text=True)  # fmt: skip
    return sum('"event": "cycle"' in line for line in (logs.stdout + logs.stderr).splitlines())


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_next_second() -> str:
    """The next whole second: `docker logs --since` takes whole seconds, so a moment
    rounded down would count lines logged earlier in that second."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(time.time()) + 1))


def inject(body: dict[str, Any]) -> str:
    status, fault = http(f"{AGENT}/faults", "POST", signer().sign(fault_request(body)))
    if status != 201:
        raise AssertionError(f"injecting {body['type']} failed: {status} {fault}")
    print(f"  injected {body['type']} on {body['target']} at {fault['onset']}")
    return str(fault["fault_id"])


def revert(fault_id: str) -> None:
    status, fault = http(f"{AGENT}/faults/{fault_id}", "DELETE")
    if status != 200 or fault["status"] != "reverted":
        raise AssertionError(f"revert failed: {status} {fault}")


def latency() -> None:
    baseline = statistics.median(readyz_seconds() for _ in range(5))
    fault = inject({"type": "latency", "target": "ledger-db-proxy", "latency_ms": LATENCY_MS})
    slowed = statistics.median(readyz_seconds() for _ in range(5))
    print(f"  readyz {baseline * 1000:.0f} ms -> {slowed * 1000:.0f} ms")
    expect(slowed - baseline >= LATENCY_MS / 1000 * 0.8, "latency not observed")
    revert(fault)
    eventually(lambda: readyz_seconds() < baseline + 0.2, "readyz is fast again")


def proxy_outage(kind: str) -> None:
    fault = inject({"type": kind, "target": "ledger-db-proxy"})
    eventually(lambda: not ready(timeout=3), f"{kind} makes payments-api unready")
    revert(fault)
    eventually(ready, "payments-api is ready again")


def partition() -> None:
    fault = inject({"type": "network_partition", "target": "ledger-db", "network": "sandbox_net",
                    "required_label": "rai.target=ledger-db"})  # fmt: skip
    eventually(lambda: not ready(timeout=3), "the partition makes payments-api unready")
    revert(fault)
    eventually(ready, "payments-api is ready again")


def stop() -> None:
    fault = inject({"type": "container_stop", "target": "payments-api",
                    "required_label": "rai.target=payments-api"})  # fmt: skip
    expect(inspect("payments-api", "{{.State.Running}}") == "false", "payments-api still runs")
    expect(not ready(), "payments-api still answers")
    revert(fault)
    eventually(ready, "payments-api is back", within=90)


def restart() -> None:
    before = inspect("ledger-db", "{{.State.StartedAt}}")
    inject({"type": "container_restart", "target": "ledger-db",
            "required_label": "rai.target=ledger-db"})  # fmt: skip
    after = inspect("ledger-db", "{{.State.StartedAt}}")
    print(f"  ledger-db started {before} -> {after}")
    expect(after != before, "restart not observed")
    eventually(ready, "payments-api is ready again", within=90)


def stale() -> None:
    eventually(lambda: etl_cycles("1m") > 0, "the ETL is cycling")
    fault = inject({"type": "stale_upstream", "target": "etl-job",
                    "required_label": "rai.target=etl-job"})  # fmt: skip
    expect(inspect("etl-job", "{{.State.Paused}}") == "true", "etl-job is not paused")
    paused_at = utc_next_second()  # a cycle logged just before the pause is not "after"
    time.sleep(8)  # four ETL intervals
    expect(etl_cycles(paused_at) == 0, "the paused ETL still cycled")
    print("  no ETL cycle for 8 s while paused")
    revert(fault)
    resumed_at = utc_now()
    eventually(lambda: etl_cycles(resumed_at) > 0, "the ETL cycles again")


def refused() -> None:
    """The agent's own checks: an unlabelled or non-sandbox target is refused."""
    status, answer = http(f"{AGENT}/faults", "POST", signer().sign(fault_request({
        "type": "container_stop", "target": "rai-chaos-agent", "duration_seconds": 5,
        "required_label": "rai.target=rai-chaos-agent"})))  # fmt: skip
    expect(status == 422, f"not refused: {status} {answer}")
    print(f"  refused: {answer['error']}")


def signatures() -> None:
    """Threat S-03 on the real agent: only a trusted, fresh, unused signed request for
    this environment is applied. Nothing below may leave a fault behind."""
    drop = {"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 5}
    used = signer().sign(fault_request(drop))
    expect(http(f"{AGENT}/faults", "POST", used)[0] == 201, "a valid signed request failed")
    http(f"{AGENT}/kill", "POST", {})
    last = used["signature"][-4:]
    forged = used | {"signature": used["signature"][:-4] + ("AAAA" if last != "AAAA" else "BBBB")}
    stale = datetime.now(UTC) - timedelta(minutes=3)
    elsewhere = fault_request(drop) | {"environment": "STAGING-01"}
    too_long = fault_request(drop | {"duration_seconds": 3600, "max_duration_seconds": 3600})
    cases = {
        "unsigned": (fault_request(drop), 403),
        "another key": (Signer.generate().sign(fault_request(drop)), 403),
        "forged signature": (forged, 403),
        "replayed": (used, 409),
        "expired": (signer().sign(fault_request(drop), now=stale), 403),
        "other environment": (signer().sign(elsewhere), 403),
        "over the agent's ceiling": (signer().sign(too_long), 422),
    }
    for name, (envelope, wanted) in cases.items():
        status, answer = http(f"{AGENT}/faults", "POST", envelope)
        error = answer.get("error") if isinstance(answer, dict) else answer
        print(f"  {name}: {status} {error}")
        expect(status == wanted, f"{name}: expected {wanted}, got {status}")
    expect(http(f"{AGENT}/faults")[1]["faults"] == [], "a refused request left a fault open")
    eventually(ready, "payments-api is ready again after the dropped connection")


PROBE = """
import json, sys, urllib.request, urllib.error
results = []
for method, path in (("POST", "/v1.41/containers/create"), ("POST", "/v1.41/images/create"),
                     ("POST", "/v1.41/containers/x/exec"), ("GET", "/v1.41/volumes"),
                     ("GET", "/v1.41/containers/json")):
    req = urllib.request.Request("http://socket-proxy:2375" + path, data=b"{}" if method ==
        "POST" else None, method=method, headers={"Content-Type": "application/json"})
    try:
        results.append(urllib.request.urlopen(req, timeout=5).status)
    except urllib.error.HTTPError as error:
        results.append(error.code)
print(json.dumps(results))
"""


def socket_proxy() -> None:
    """Threat E-01: from the agent itself, everything but the listed verbs is refused."""
    out = subprocess.run(  # noqa: S603
        ["docker", "compose", "exec", "-T", "rai-chaos-agent", "python", "-c", PROBE],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    create, pull, exec_, volumes, listing = json.loads(out)
    print(f"  create {create}, image pull {pull}, exec {exec_}, volumes {volumes}, list {listing}")
    expect(all(code in (403, 405) for code in (create, pull, exec_, volumes)),
           "the socket proxy let a forbidden call through")  # fmt: skip
    expect(listing == 200, "the socket proxy refused an allowed call")


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], check=True,  # noqa: S603, S607
                          capture_output=True, text=True).stdout  # fmt: skip


def history() -> dict[str, dict[str, Any]]:
    """Every fault in the state file, read from the watchdog (it works with the agent dead)."""
    out = compose("exec", "-T", "rai-chaos-watchdog", "rai-chaos-agent", "status", "--all")
    return {f["fault_id"]: f for f in json.loads(out)}


def agent_up() -> bool:
    try:
        return http(f"{AGENT}/healthz", timeout=2)[0] == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def watchdog_after_agent_crash() -> None:
    """D-01: the agent dies with a fault active; the watchdog reverts it after its
    duration plus the grace, with nobody else acting."""
    expect("rai-chaos-watchdog" in compose("ps", "--status", "running", "--services"),
           "the watchdog is not running")  # fmt: skip
    fault = inject({"type": "container_stop", "target": "payments-api", "duration_seconds": 5,
                    "required_label": "rai.target=payments-api"})  # fmt: skip
    killed = time.monotonic()
    compose("kill", "rai-chaos-agent")
    try:
        expect(not agent_up(), "the agent still answers after being killed")
        expect(not ready(), "payments-api answers while stopped")
        eventually(ready, "the watchdog restores payments-api", within=90)
        print(f"  agent killed; payments-api back {time.monotonic() - killed:.0f} s later "
              "(duration 5 s + grace)")  # fmt: skip
        record = history()[fault]
        expect(record["reverted_by"] == "watchdog", f"reverted by {record['reverted_by']}")
        print(f"  state file: reverted at {record['reverted_at']} by the watchdog")
    finally:
        compose("start", "rai-chaos-agent")
        eventually(agent_up, "the agent is back")


def kill_switch_drill() -> None:
    """FR-07.3: `kill --all` from the state file reverts every open fault with no RAI API
    and no database in the stack, and without the agent's HTTP server."""
    running = compose("ps", "--status", "running", "--services").split()
    expect("rai-db" not in running, "rai-db is running; the drill needs it down")
    print(f"  running: {', '.join(sorted(running))} (no RAI API, no rai-db)")
    both = ["etl-job", "ledger-db-proxy"]
    drill = f"drill-{uuid.uuid4()}"
    for body in ({"type": "stale_upstream", "target": "etl-job",
                  "required_label": "rai.target=etl-job"},
                 {"type": "connection_drop", "target": "ledger-db-proxy"}):  # fmt: skip
        inject(body | {"duration_seconds": 600, "run_id": drill, "blast_radius": both})
    expect(inspect("etl-job", "{{.State.Paused}}") == "true", "etl-job is not paused")
    eventually(lambda: not ready(timeout=3), "payments-api is down")
    start = time.monotonic()
    out = compose("exec", "-T", "rai-chaos-agent", "rai-chaos-agent", "kill", "--all")
    seconds = time.monotonic() - start
    print(f"  kill --all: {out.strip()} in {seconds:.1f} s")
    expect("reverted 2 fault(s)" in out, "kill --all did not revert both faults")
    expect(inspect("etl-job", "{{.State.Paused}}") == "false", "etl-job is still paused")
    eventually(ready, "payments-api is ready again")
    kinds = {f["request"]["run_id"]: f["reverted_by"] for f in history().values()
             if f["request"]["run_id"] == drill}  # fmt: skip
    expect(kinds == {drill: "kill"}, f"not recorded as a kill: {kinds}")


CHECKS: list[tuple[str, Callable[[], None]]] = [
    ("socket_proxy", socket_proxy),
    ("signatures", signatures),
    ("latency", latency),
    ("timeout", lambda: proxy_outage("timeout")),
    ("connection_drop", lambda: proxy_outage("connection_drop")),
    ("network_partition", partition),
    ("container_stop", stop),
    ("container_restart", restart),
    ("stale_upstream", stale),
    ("refusal", refused),
    ("watchdog_after_agent_crash", watchdog_after_agent_crash),
    ("kill_switch_drill", kill_switch_drill),
]


def main() -> int:
    eventually(lambda: http(f"{AGENT}/healthz")[0] == 200, "the agent is up")
    eventually(ready, "payments-api is ready")
    failed = []
    for name, check in CHECKS:
        print(f"== {name}")
        try:
            check()
            print("  ok")
        except Exception as error:  # report every failure, then carry on with the next type
            print(f"  FAILED: {error}")
            failed.append(name)
            if agent_up():
                http(f"{AGENT}/kill", "POST", {})
    left = http(f"{AGENT}/faults")[1]["faults"]
    print(f"open faults left: {len(left)}")
    print("all fault types observed and reverted" if not failed else f"failed: {failed}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
