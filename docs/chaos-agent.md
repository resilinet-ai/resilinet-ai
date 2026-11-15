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

## What the agent checks itself

The agent does not trust whoever asks. Before acting, it checks:

- **The request:** a known fault type, and a duration of 1 s up to the environment's ceiling (never more than 60 minutes).
- **A container target:** exactly one container of that Compose service, in the agent's own Compose project. It must carry `rai.sandbox=true` and the label it was registered with (`required_label`, B-22). The agent reads its own project from its container's labels; if it cannot, it refuses all container faults.
- **A proxy target:** the proxy exists in Toxiproxy.
- **One fault per target.**

**Recorded before acting.** Every fault is written to the agent's state file first, then marked active with its onset (t0), or marked failed and undone. Faults past their duration are reverted automatically. Faults left open by a crash are reverted when the agent starts.

## Least privilege

- **The Docker socket** is mounted only by the socket proxy (`wollomatic/socket-proxy`). It allows only listing and inspecting containers; stop, start, restart, pause and unpause; and network connect and disconnect. Only the agent may connect to it. Container creation, exec, image pulls and volume access are refused; CI checks this on every pull request.
- **Networks.** The socket proxy and Toxiproxy's admin API are on `chaos_net`, an internal network with no route out, shared only with the agent. The agent also joins `bridge_net`, where the worker will reach it.
- **Opt-in profile.** The agent and the socket proxy are in the `chaos` profile, so the socket is mounted only when chaos is asked for.

## Running it

```bash
docker compose --profile sandbox --profile chaos up -d --build
docker compose exec rai-chaos-agent rai-chaos-agent status     # open faults
docker compose exec rai-chaos-agent rai-chaos-agent kill --all # revert everything
curl -s -X POST 127.0.0.1:18090/kill                           # the same, from the host
```

**Unsigned requests are refused.** Fault requests must be signed by the worker, which arrives with B-24. Until then, a deployed agent injects nothing: `POST /faults` answers 403.

**For tests and CI only,** `RAI_AGENT_UNSIGNED_DEV=1` allows unsigned requests:

```bash
make chaos-e2e   # every fault type, its effect on SandboxBank, and its recovery
```

On Linux, set `DOCKER_GID` to the group that owns `/var/run/docker.sock` (`stat -c %g /var/run/docker.sock`); Docker Desktop needs nothing.

## HTTP interface

| Request | Answer |
| --- | --- |
| `POST /faults` | `201` with the fault and its `onset`; `422` refused, with the reason; `403` unsigned |
| `DELETE /faults/{id}` | `200`: reverted |
| `GET /faults` | Open faults |
| `POST /kill` | Reverts every open fault; always allowed |
| `GET /healthz` | `{"status": "ok"}` |

Example request body: `{"type": "container_stop", "target": "payments-api", "duration_seconds": 60, "required_label": "rai.target=payments-api"}`.

**Coming next:**
- **B-24:** worker-signed requests and the agent's full second preflight.
- **B-25:** the independent watchdog and the kill-switch drill with the API and database down.
