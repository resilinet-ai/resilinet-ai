# Run a chaos scenario

**Goal:** inject a declared fault into SandboxBank and measure whether it recovers within its RTO and RPO objectives (FR-07, FR-08).

**Who:** an **admin** registers where faults may run; an **engineer** writes the scenario; a **reviewer** who did not write it approves it. Nobody can approve their own scenario, and the database enforces that too.

**You need:** the quick start's platform (part 2), plus Docker for SandboxBank and the chaos agent. Keep [stop every fault now](stop-all-faults.md) open in another tab.

## What can never happen

Faults run only in a registered SANDBOX or STAGING environment, only against targets on its allow-list, inside the scenario's declared blast radius, for at most the environment's ceiling; the agent applies only requests signed by a worker key it trusts; and the watchdog reverts any fault that outlives its time. Every refusal is listed in the [refusal catalogue](../refusals.md).

## 1. Start SandboxBank and the chaos agent

Make the worker's signing key; the command prints the public half:

```bash
mkdir -p .rai/chaos
uv run rai admin chaos-keygen --out .rai/chaos/worker.pem
```

Put the printed public key in `.env` as `RAI_AGENT_WORKER_KEYS=...`. Write it in the file, not on the command line: Git Bash on Windows rewrites a value that starts with `/` into a Windows path before Docker sees it. Then:

```bash
docker compose --profile core --profile sandbox --profile chaos up -d --build --wait
curl -s 127.0.0.1:18090/healthz        # the agent: {"status": "ok"}
curl -s 127.0.0.1:18080/readyz         # SandboxBank's payments API: {"status": "ready"}
```

The agent serves one environment, `SANDBOXBANK` (`RAI_AGENT_ENVIRONMENT`). Without a trusted key it refuses every fault.

Restart the worker so it can sign fault requests. Secret references need an absolute path (`$(pwd -W)` instead of `$PWD` in Git Bash):

```bash
RAI_WORKER_CONNECTORS=.rai/quickstart/connectors \
RAI_WORKER_SIGNING_KEY="file:$PWD/.rai/chaos/worker.pem" \
RAI_WORKER_CHAOS_AGENT_URL=http://127.0.0.1:18090 \
uv run --env-file .env rai worker
```

## 2. Register the environment and its allow-list (admin)

The environment's name must be the agent's, and its ceiling bounds every fault:

```bash
curl -s -X POST "$API/v1/environments" -H "$ADMIN_AUTH" -H 'content-type: application/json' \
  -d '{"name": "SANDBOXBANK", "type": "SANDBOX", "max_fault_duration_seconds": 300}'
```

Allow the targets this scenario touches, `POST /v1/environments/<environment id>/targets` once for each:

```json
{"kind": "proxy", "name": "ledger-db-proxy", "required_label": "rai.target=toxiproxy"}
{"kind": "http", "name": "payments-health", "health_url": "http://127.0.0.1:18080/readyz"}
{"kind": "http", "name": "payments-ledger", "health_url": "http://127.0.0.1:18080/v1/ledger/head"}
```

The first is the Toxiproxy route between SandboxBank's payments API and its ledger database. The other two are what recovery is measured by: the API's readiness, and its ledger head (the last committed transaction). The worker runs on your machine here, so the health URLs use the host ports.

## 3. Write the scenario (engineer) and review it (reviewer)

```bash
curl -s -X POST "$API/v1/projects/<project id>/scenarios" -H "$AUTH" -H 'content-type: application/json' -d '{
  "name": "ledger-connection-drop",
  "description": "drop the ledger connection for 20 s; payments must come back with nothing lost",
  "blast_radius": ["ledger-db-proxy"],
  "faults": [{"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 20}],
  "recovery": {"probes": ["payments-health"], "ledger": "payments-ledger",
               "rto_seconds": 60, "rpo_seconds": 0, "window_seconds": 120, "stable_checks": 3}}'
```

It starts `pending`. The reviewer reads it (the radius, each fault, the objectives) and decides:

```bash
curl -s -X POST "$API/v1/scenarios/<scenario id>/approvals" -H "$REVIEWER_AUTH" \
  -H 'content-type: application/json' -d '{"decision": "approve", "comment": "one proxy, 20 s, inside the ceiling"}'
```

A later `reject` withdraws the approval; a changed scenario is a new version that needs its own.

## 4. Run it (engineer)

A plan with the scenario, in that environment, then a run:

```bash
curl -s -X POST "$API/v1/projects/<project id>/plans" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name": "ledger-resilience", "environment_id": "<environment id>",
       "items": [{"item_type": "scenario", "scenario_id": "<scenario id>"}]}'
curl -s -X POST "$API/v1/assurance/runs" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"plan_id": "<plan id>"}'
```

The worker checks everything again, signs the fault, and the agent drops the connection. For 20 s the payments API is not ready. Then the worker reverts the fault and probes until the API answers three times in a row.

## Check it worked

The run ends `passed` after about 25 s. Its observations, on the run's page or from `/observations`:

| Observation | What it says |
| --- | --- |
| `recovery.ledger_before` | The ledger head before the fault |
| `fault.onset`, `fault.reverted` (step `item-1.fault-1`) | When the fault started and ended, who signed it, who reverted it |
| `recovery.rto` | Seconds from the fault's onset to validated restoration: about 22 here, against 60 |
| `recovery.rpo` | Seconds of committed data lost, and rows lost: 0 and 0 |

`GET /v1/assurance/recovery?project_id=...` and the Dashboard's RTO and RPO cards count it: `1 / 1`. A missed objective, no restoration within the window, or lost rows fail the step with the reason.

## If it goes wrong

| Symptom | Cause |
| --- | --- |
| Run `refused` | Preflight found a problem before anything was injected: the scenario is not approved, a target is not on the allow-list or is the wrong kind, the duration exceeds the ceiling, or the worker has no signing key. The reason lists them all. |
| Step `error`, `fault.refused` | The agent refused the request: an untrusted key, another environment, a target outside the radius. See the [refusal catalogue](../refusals.md). |
| Step `error`, "could not read the ledger head" | The `payments-ledger` URL is wrong or SandboxBank is down. Nothing was injected. |
| Anything looks wrong while a fault is open | [Stop every fault now](stop-all-faults.md). |

To prove the system comes back *correct* and not just healthy, add a consistency check after the scenario in the same plan (FR-08.3), and for RPO ground truth, SandboxBank's seeding log (B-30). Both are in the [API reference](../api.md#the-worker).
