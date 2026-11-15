"""Each fault type, observable on SandboxBank (B-23: FR-07.1 acceptance). Synthetic only.

Run against the Compose stack with the chaos profile and unsigned development
requests allowed (the CI job "Chaos agent" does exactly this):

    RAI_AGENT_UNSIGNED_DEV=1 ETL_INTERVAL_SECONDS=2 \\
      docker compose --profile sandbox --profile chaos up -d --build --wait
    uv run python scripts/chaos_e2e.py

For each of the seven fault types it injects one fault through the agent's HTTP
interface, shows its effect on SandboxBank, reverts it, and shows the recovery.
Exit code 0 only if every fault had its effect and every effect went away.
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
from collections.abc import Callable
from typing import Any

AGENT = os.environ.get("RAI_AGENT_URL", "http://127.0.0.1:18090")
PAYMENTS = os.environ.get("SANDBOXBANK_URL", "http://127.0.0.1:18080")
LATENCY_MS = 500


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


def inject(body: dict[str, Any]) -> str:
    status, fault = http(f"{AGENT}/faults", "POST", {"duration_seconds": 120} | body)
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
    paused_at = utc_now()
    time.sleep(8)  # four ETL intervals
    expect(etl_cycles(paused_at) == 0, "the paused ETL still cycled")
    print("  no ETL cycle for 8 s while paused")
    revert(fault)
    resumed_at = utc_now()
    eventually(lambda: etl_cycles(resumed_at) > 0, "the ETL cycles again")


def refused() -> None:
    """The agent's own checks: an unlabelled or non-sandbox target is refused."""
    status, answer = http(f"{AGENT}/faults", "POST", {
        "type": "container_stop", "target": "rai-chaos-agent", "duration_seconds": 5,
        "required_label": "rai.target=rai-chaos-agent"})  # fmt: skip
    expect(status == 422, f"not refused: {status} {answer}")
    print(f"  refused: {answer['error']}")


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


CHECKS: list[tuple[str, Callable[[], None]]] = [
    ("socket_proxy", socket_proxy),
    ("latency", latency),
    ("timeout", lambda: proxy_outage("timeout")),
    ("connection_drop", lambda: proxy_outage("connection_drop")),
    ("network_partition", partition),
    ("container_stop", stop),
    ("container_restart", restart),
    ("stale_upstream", stale),
    ("refusal", refused),
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
            http(f"{AGENT}/kill", "POST", {})
    left = http(f"{AGENT}/faults")[1]["faults"]
    print(f"open faults left: {len(left)}")
    print("all fault types observed and reverted" if not failed else f"failed: {failed}")
    return 1 if failed or left else 0


if __name__ == "__main__":
    sys.exit(main())
