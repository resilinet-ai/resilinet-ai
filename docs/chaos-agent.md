# Chaos agent

`rai-chaos-agent` is the only component that injects faults (FR-07.1). It runs inside the SandboxBank Compose stack, acts only on that stack, and refuses anything it cannot verify itself. Synthetic data only.

## Fault types

The protocol's seven types (Validation & Benchmark Protocol, section 5). Scenarios S01 to S20 use them alone or in sequence.

| Type | Target | What the agent does | Revert |
| --- | --- | --- | --- |
| `latency` | a Toxiproxy proxy | Adds a latency toxic (`latency_ms`, optional `jitter_ms`) | Removes it |
| `timeout` | a Toxiproxy proxy | Adds a timeout toxic: data stops, so calls hang until the client gives up | Removes it |
| `connection_drop` | a Toxiproxy proxy | Disables the proxy: open connections close, new ones are refused | Enables it |
| `network_partition` | a container | Disconnects it from a network (`network`) | Reconnects it with the same DNS aliases |
| `container_stop` | a container | Stops it | Starts it |
| `container_restart` | a container | Restarts it | Nothing to revert (instant) |
| `stale_upstream` | a producer container | Pauses it: its output stops while it keeps its state | Unpauses it |

Proxies in the stack: `ledger-db-proxy` (payments-api and the ETL to ledger-db) and `analytics-db-proxy` (the ETL to analytics-db). SandboxBank's database traffic always goes through them, so network faults reach the real paths.

## Who may ask: signed requests (B-24)

The agent applies a fault only if the worker signed the request. There is no unsigned mode.

1. **The worker checks first.** The run must have an active environment, and the duration must be within that environment's ceiling. Every target in the declared blast radius must be on the environment's allow-list with the right kind: `proxy` for latency, timeout and connection drop, `container` for the rest. The label the target must carry comes from the registry, never from the test definition.
2. **The worker signs.** The key is Ed25519, made by `rai admin chaos-keygen`. The request names its run, step, tenant and environment, and is valid for 60 s.
3. **The agent verifies.** The key must be one it trusts (`RAI_AGENT_WORKER_KEYS`), and the signature must match the exact bytes sent. The request must be fresh: at most 120 s old, with 30 s allowed for clock differences. It must also be for the agent's own environment (`RAI_AGENT_ENVIRONMENT`). If any of this fails, the answer is `403`.
4. **Used once.** One fault per run and step, ever. A repeat is answered `409` with the fault already recorded. A worker retrying a step after a crash records that same onset, once.
5. **The worker records the outcome** as an observation of the run: `fault.onset`, `fault.refused` (by the worker or the agent, with the reasons) or `fault.error`.

```bash
uv run rai admin chaos-keygen --out /run/secrets/rai_worker_signing_key   # prints the public key
# worker: RAI_WORKER_SIGNING_KEY=file:/run/secrets/rai_worker_signing_key
#         RAI_WORKER_CHAOS_AGENT_URL=http://rai-chaos-agent:8090
# agent:  RAI_AGENT_WORKER_KEYS=<the printed public key>[,<another>]
```

## What the agent checks itself

The agent does not trust whoever asks, even a signed request. Before acting, it checks:

- **The request:** a known fault type, and a duration of 1 s up to the smaller of the environment's ceiling and the agent's own (`RAI_AGENT_MAX_FAULT_SECONDS`, default 900 s; never more than 60 minutes).
- **The blast radius:** the target is in the radius the scenario declared, a run keeps the radius of its first fault, and at most `RAI_AGENT_MAX_OPEN_FAULTS` faults (default 3) are open at once.
- **A container target:** exactly one container of that Compose service, in the agent's own Compose project. It must carry `rai.sandbox=true` and the label it was registered with (`required_label`, B-22). The agent reads its own project from its container's labels; if it cannot, it refuses all container faults.
- **A proxy target:** the proxy exists in Toxiproxy.
- **One fault per target.**

**Recorded before acting.** Every fault is written to the agent's state file first, then marked active with its onset (t0), or marked failed and undone. Faults past their duration are reverted automatically. Faults left open by a crash are reverted when the agent starts. Every revert records who made it (`reverted_by`): `request`, `expiry`, `kill`, `startup` or `watchdog`.

## Getting faults out: the watchdog and the kill switch (B-25)

A fault must end even if the agent does not.

- **The watchdog** (`rai-chaos-watchdog`) is a second process in its own container, built from the same image. It shares only the state volume and the two backends. It has no HTTP interface and is not on `bridge_net`, so nothing can ask it to inject. Every 5 s it reverts any open fault past its deadline. The deadline is the time of the request plus the fault's duration plus a grace period (`RAI_WATCHDOG_GRACE_SECONDS`, default 30 s). Counting from the request means a fault the agent left half-injected when it died is covered too. The grace lets the agent's own expiry act first in normal running. Its health check fails if a sweep has not finished within the last minute, and it restarts with Docker.
- **One lock.** The agent and the watchdog take the same lock on the state file across processes (`flock` on `state.json.lock`). The agent holds it for a whole check, record and act sequence, so the watchdog never undoes an injection in progress. If the lock stays held for more than 20 s, the agent is presumed hung, and the watchdog reverts overdue faults anyway. Every revert is idempotent, and a revert only ever makes the sandbox healthier.
- **The kill switch.** `rai-chaos-agent kill --all` reverts every open fault from the state file. It needs no RAI API, no database and not even the agent's HTTP server.
- **The drill** (in `make chaos-e2e` and the CI job "Chaos agent"):
  1. The agent is killed with `payments-api` stopped. The watchdog restarts `payments-api` once the fault's duration and grace have passed, and the state file shows it as reverted by the watchdog.
  2. With no RAI API or database in the stack, two 10-minute faults are open. `kill --all` reverts both.

## Least privilege

- **The Docker socket** is mounted only by the socket proxy (`wollomatic/socket-proxy`). It allows only listing and inspecting containers; stop, start, restart, pause and unpause; and network connect and disconnect. Only the agent and the watchdog may connect to it. Container creation, exec, image pulls and volume access are refused; CI checks this on every pull request.
- **Networks.** The socket proxy and Toxiproxy's admin API are on `chaos_net`, an internal network with no route out, shared only with the agent and the watchdog. The agent also joins `bridge_net`, where the worker reaches it; the watchdog does not.
- **Opt-in profile.** The agent and the socket proxy are in the `chaos` profile, so the socket is mounted only when chaos is asked for.
- **Pinned dependency.** The agent's one dependency, `cryptography`, is installed from `requirements.txt` with every hash pinned. That file is exported from `uv.lock`, and a test keeps the two in step.

## Running it

```bash
docker compose --profile sandbox --profile chaos up -d --build
docker compose exec rai-chaos-agent rai-chaos-agent status     # open faults (--all: every fault)
docker compose exec rai-chaos-agent rai-chaos-agent kill --all # revert everything
curl -s -X POST 127.0.0.1:18090/kill                           # the same, from the host
```

**Without a trusted key, nothing is injected.** If `RAI_AGENT_WORKER_KEYS` is empty, every `POST /faults` answers 403.

The end-to-end check makes a throwaway worker key, starts the stack trusting only that key, and signs every request with it:

```bash
make chaos-e2e   # every fault type, its effect and recovery; forged, replayed and expired
                 # requests refused; the watchdog after the agent is killed; kill --all
```

On Linux, set `DOCKER_GID` to the group that owns `/var/run/docker.sock` (`stat -c %g /var/run/docker.sock`); Docker Desktop needs nothing.

## HTTP interface

| Request | Answer |
| --- | --- |
| `POST /faults` | A signed envelope. `201` with the fault and its `onset`; `403` unsigned, untrusted key, bad signature, expired or another environment; `409` the run and step already had a fault (the answer carries it); `422` refused by the agent's checks, with the reason |
| `DELETE /faults/{id}` | `200`: reverted |
| `GET /faults` | Open faults |
| `POST /kill` | Reverts every open fault; always allowed |
| `GET /healthz` | `{"status": "ok"}` |

The envelope is `{"format": "rai-signed-fault/1", "key_id": ..., "payload": ..., "signature": ...}`. Its payload is `{"format": "rai-fault-request/1", "issued_at": ..., "expires_at": ..., "request": {...}}`, and the request inside looks like this: `{"type": "container_stop", "target": "payments-api", "duration_seconds": 60, "required_label": "rai.target=payments-api", "run_id": ..., "step_key": "item-1", "tenant_id": ..., "environment": "SANDBOXBANK", "max_duration_seconds": 900, "blast_radius": ["payments-api"]}`. See `chaos_agent/signing.py`.

**Scenarios (B-26).** Runs reach the agent through approved chaos scenarios: the worker signs one request per fault, holds it for its duration and then asks the agent to revert it (see [Chaos scenarios](api.md#chaos-scenarios)).

**Coming next:** measuring recovery (RTO and RPO, FR-08.1 and FR-08.2) from each fault's onset and revert, and the scenario-level blast radius (B-27).
