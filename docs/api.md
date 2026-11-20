# RAI API

The REST API under `/v1` (Data Model sections 7 and 8). B-16 added sign-in, tenants and members; B-17 projects and test definitions; B-18 runs and the worker; B-19 lineage; B-20 API keys and the CI gate; B-22 environments; later items add evidence. Synthetic data only.

## Run it locally

```bash
docker compose --profile core up -d          # rai-db on 127.0.0.1:55431
uv run rai db upgrade                        # as the owner (RAI_DATABASE_URL)
uv run rai db set-api-password               # password of the rai_api login role
uv run rai admin create-tenant --slug demo-cu --name "Demo CU" \
    --admin-email admin@example.test --admin-name "Demo Admin"
uv run rai serve                             # 127.0.0.1:8000
uv run rai db set-worker-password            # password of the rai_worker login role
uv run rai worker                            # in a second terminal: executes queued runs
```

`rai serve` needs `RAI_API_DATABASE_URL` (logging in as `rai_api`) and `RAI_JWT_SECRET` (at least 32 bytes) or `RAI_JWT_SECRET_FILE`; see `.env.example`. Passwords are read from `RAI_NEW_PASSWORD` / `RAI_API_DB_PASSWORD` or a hidden prompt, never from command-line arguments.

**Behind TLS.** The compose `core` profile runs the API behind Caddy, which serves HTTPS only (B-36); see [deployment.md](deployment.md). Behind any proxy, start the API with `rai serve --forwarded-allow-ips <proxy address>` (or `RAI_FORWARDED_ALLOW_IPS`) so the per-address login limit sees each client; without it, forwarded headers are ignored.

**The API refuses to start** if it logs in as a superuser, the schema owner, or a role that bypasses row-level security. As `rai_api`, a query without a tenant context sees no rows at all.

## Sign in

```bash
curl -s localhost:8000/v1/auth/login -H 'content-type: application/json' \
  -d '{"email": "admin@example.test", "password": "..."}'
# {"access_token": "...", "token_type": "bearer", "expires_in": 900, "refresh_token": "rairt_..."}
curl -s localhost:8000/v1/me -H "authorization: Bearer $ACCESS"
```

- **Access token:** a JWT valid for 15 minutes. It names only the user; tenant and role are checked on every request.
- **Refresh token:** `POST /v1/auth/refresh` returns a new pair, and the old refresh token stops working. Presenting an old one again ends the whole session. Sessions last at most 7 days from sign-in.
- **Failed sign-ins** all get the same 401, whether the email is unknown, the password is wrong or the account is not active. After 5 failures for one email within 15 minutes, further attempts get 429 with `Retry-After`.

## Tenants and roles

Send `X-RAI-Tenant: <tenant id>` if you belong to several tenants. With one tenant it is optional.

| Endpoint | Roles | Notes |
| --- | --- | --- |
| `POST /v1/auth/login`, `/refresh` | public | |
| `POST /v1/auth/logout` | signed in | Revokes the session of the given refresh token |
| `GET /v1/me`, `GET /v1/tenants` | signed in | Own profile and memberships |
| `GET /v1/members` | any role in the tenant | |
| `POST /v1/members` | admin | Invite: always `202`, whatever the email |
| `PATCH /v1/members/{user_id}` | admin | Change role; not your own; `409` if the tenant would lose its last admin |
| `DELETE /v1/members/{user_id}` | admin | Remove; not yourself |

- **Another tenant, or anything in it,** is `404 Not found`, never `403`: the answer does not reveal that it exists.
- **A wrong role** is `403`.
- **Tenants are created** with `rai admin create-tenant`, because the API's database role may not create tenants.
- **An invited account** gets its first password from the operator (`rai admin set-password`) until self-service activation exists.

## Environments and targets

Where RAI may act (NFR-SAF-1). **Production cannot be registered**: the database's environment type has only SANDBOX and STAGING, `production_flag` can only be false, and names may not contain `PROD`.

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `GET, POST /v1/environments` | admin | `{"name": "SANDBOX-01", "type": "SANDBOX", "max_fault_duration_seconds": 900}`; `?status=` |
| `GET, PATCH /v1/environments/{id}` | admin | PATCH changes only `max_fault_duration_seconds` (at most 3,600) and `status`; needs `If-Match` |
| `GET, POST /v1/environments/{id}/targets` | admin | The allow-list of injectable and probeable targets |
| `DELETE /v1/environments/{id}/targets/{target_id}` | admin | Take a target off the allow-list |

- **Fixed at registration:** an environment's name and type never change. To change them, register a new environment and retire the old one (`"status": "retired"`). Environments are never deleted.
- **Retired environments** take no new targets, and no new run starts there (`409`).
- **Targets:** `kind` is `container`, `proxy`, `db` or `http`. Containers and proxies must name the label they carry (`required_label`, e.g. `rai.target=ledger-db`), which the chaos agent checks before it acts. An optional `health_url` is where recovery probes look.
- **Every role in the tenant can read**; every change is audited.

**Where runs execute.** A project may name a `default_environment_id`, and a plan its own `environment_id`. A run pins the plan's environment, or else the project's default (`pins.environment`, `environment_id` on the run).

**Deny by default.** The worker refuses a test whose `environment` is not a registered, active environment of the tenant, or is not the run's pinned environment.

## Projects and test definitions

What a run will test. Contracts, test cases and plans are **immutable once published**: publishing again creates the next version, and a run pins the exact version.

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `GET, POST /v1/projects` | admin, engineer | `?status=active` filter |
| `GET, PATCH /v1/projects/{id}` | admin, engineer | PATCH needs `If-Match` with the `ETag` from GET |
| `GET, POST /v1/projects/{id}/assets` | engineer | Asset names are unique per project |
| `GET /v1/assets/{id}` | | |
| `GET, POST /v1/assets/{id}/contracts` | engineer | Body `{"source": "<contract YAML or JSON>"}` |
| `GET /v1/assets/{id}/contracts/{version}` | | Full spec and the source as published |
| `GET, POST /v1/projects/{id}/test-cases` | engineer | Next version per `name`; `?name=` lists its versions |
| `GET, POST /v1/projects/{id}/plans` | engineer | Ordered items: test cases (optionally pinned to a contract version) and chaos scenarios (`{"item_type": "scenario", "scenario_id": ...}`) |
| `GET, POST /v1/projects/{id}/scenarios` | engineer | Chaos scenarios: next version per `name`; see [Chaos scenarios](#chaos-scenarios) |
| `GET /v1/scenarios/{id}` | | The version, its status and every review decision |
| `POST /v1/scenarios/{id}/approvals` | reviewer, admin | `{"decision": "approve" or "reject", "comment": ...}`; never by the scenario's author |
| `GET, POST /v1/assets/{id}/inputs` | engineer | What the asset is derived from (see [Lineage](#lineage)) |
| `DELETE /v1/assets/{id}/inputs/{input_id}` | engineer | Remove a wrong declaration; queued runs keep their pinned graph |

Every role in the tenant can read.

**Publishing a contract:**
- It is checked exactly as `rai contract check` checks it; problems come back in `errors`.
- Its `version:` must be higher than the latest published one.
- Its `contract:` name must match earlier versions of the same asset.
- The response carries `ref` (e.g. `transactions_raw@3`) and `spec_sha256`, the same values `rai validate` writes into its reports.

**Updating a project:**

```bash
curl -s localhost:8000/v1/projects/$ID -H "authorization: Bearer $ACCESS" -i | grep -i etag
# ETag: "v1"
curl -s -X PATCH localhost:8000/v1/projects/$ID -H "authorization: Bearer $ACCESS" \
  -H 'If-Match: "v1"' -H 'content-type: application/json' -d '{"status": "archived"}'
```

- **No `If-Match`:** `428`.
- **An old `ETag`:** `412`. Read the project again.
- **An archived project** accepts no new assets or definitions (`409`).

**Lists** are paginated: `?limit=50` (at most 200), then pass `next_cursor` back as `?cursor=`. The answer is `{"items": [...], "next_cursor": null}` on the last page.

### Chaos scenarios

A scenario declares its **blast radius** (every target it may touch) and its **faults**, run in order. Every fault's target must be in the radius.

```json
{"name": "ledger-outage", "description": "drop the ledger, then stop the API",
 "blast_radius": ["ledger-db-proxy", "payments-api"],
 "faults": [{"type": "connection_drop", "target": "ledger-db-proxy", "duration_seconds": 60},
            {"type": "container_stop", "target": "payments-api", "duration_seconds": 30}]}
```

The fault types and their fields are those of the [chaos agent](chaos-agent.md): `latency` needs `latency_ms` (optional `jitter_ms`), and `network_partition` needs `network`.

- **New versions start `pending`.** A reviewer or an admin approves or rejects them. Decisions are kept, and the latest one counts, so a later `reject` withdraws an approval.
- **Separation of duties.** Nobody reviews their own scenario (`403 scenarios.self_review`). The database refuses it too.
- **Starting a run.** Every scenario in the plan must be approved (`409 runs.scenario_not_approved`), and the plan or its project must have an environment (`422 runs.scenario_needs_environment`). The run pins the scenario version and the approval it runs under.

## Runs

A run executes one plan version. It **pins every input** when it is queued: the plan, each test-case version with its definition, the target asset, the contract version with its SHA-256, and the target's declared upstream graph. The worker executes from the pins alone.

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `POST /v1/assurance/runs` | engineer | `{"plan_id": ..., "trigger_ref": "<commit SHA>"}`; answers `202` with the run |
| `GET /v1/assurance/runs` | | Newest first; `?project_id=`, `?state=`, `?since=` |
| `GET /v1/assurance/runs/{id}` | | State, verdict, reason, pins, steps, observation counts by outcome |
| `GET /v1/assurance/runs/{id}/observations` | | One per rule; `?outcome=fail` |
| `GET /v1/assurance/runs/{id}/lineage` | | Every hop with the SHA-256 of the data read (see [Lineage](#lineage)) |
| `GET /v1/assurance/recovery` | | Recovery measurements and attainment for `?project_id=` (see [Recovery measurements](#recovery-measurements)) |
| `GET /v1/assurance/summary` | | The dashboard's numbers for `?project_id=`, each with its denominator and definition (see [Dashboard summary](#dashboard-summary)) |

**States:** `queued → preflight → running → evaluating → passed | failed`.
- **`refused`:** preflight found something this worker cannot run. The reason lists every problem.
- **`error`:** the platform failed three times.
- **`aborted`:** a fault of a chaos scenario was cut short, by the kill switch or by the agent restarting, so the scenario did not run as reviewed. The run has no verdict, its reason says which fault ended and when, and nothing after it ran, recovery measurement included. `rai run` exits `2`, as for any run without a verdict.

Finished runs never change; to try again, start a new run.

**`Idempotency-Key`:** sending the same request with the same key returns the same run, which is safe for CI retries. Reusing a key for a different request is `422`.

### Recovery measurements

Every scenario step with a recovery plan records one row in `recovery_measurements`, committed in the same transaction as the step (B-29). The row holds:
- onset, revert and restoration times;
- RTO and RPO, each with the objective it was held to and whether it was met;
- the ledger rows lost.

Rows are insert-only: the database refuses UPDATE, DELETE and TRUNCATE, even from the owner. A correction is a new run.

```bash
curl -s "$API/v1/assurance/recovery?project_id=$PROJECT&since=2026-10-19T00:00:00Z" -H "$AUTH"
# {"since": "...", "until": "...", "measurements": 16, "not_restored": 1,
#  "rto": {"met": 14, "with_objective": 16, "definition": "First fault onset to ..."},
#  "rpo": {"met": 15, "with_objective": 16, "definition": "0 s if no committed ..."},
#  "items": [{"run_id": "...", "scenario_name": "ledger-resilience", "rto_seconds": "1452.300",
#             "rto_objective_seconds": 1800, "rto_met": true, ...}], "truncated": false}
```

- **Window:** onset in `[since, until)`. The default is the last 30 days; at most 366 days.
- **Filters:** `?scenario_id=` narrows the measurements; `?limit=` caps the items (at most 200; the counts always cover the whole window).
- **Denominators:** `with_objective` counts the measurements whose scenario set that objective. A run that was never restored, or whose ledger could not be read after recovery, counts as a miss, not as missing.
- **API keys:** a key with `runs:read` sees its own project only.

### Dashboard summary

Every number on the executive dashboard (FR-11.1), from one call. Each comes with its numerator, its denominator and a definition, and the window is in the answer (NFR-USE-1). There is deliberately no composite score: it would have no denominator.

```bash
curl -s "$API/v1/assurance/summary?project_id=$PROJECT&since=2026-10-19T00:00:00Z" -H "$AUTH"
# {"since": "...", "until": "...", "window_definition": "Runs and steps that finished ...",
#  "validation": {"numerator": 1153, "denominator": 1200, "definition": "Rule checks ..."},
#  "issues": {"total": 42, "rules": [{"rule": "txn_id.unique", "failing_checks": 26}, ...]},
#  "runs": {"finished": 12, "by_state": {"passed": 9, "failed": 2, "refused": 1}},
#  "refused": {"numerator": 1, "denominator": 12, ...}, "chaos": {"numerator": 3, "denominator": 4, ...},
#  "scenarios": [{"name": "etl-stale-feed", "version": 2, "run_id": "...", "verdict": "fail",
#                 "rto_met": false, "rpo_met": true, ...}],
#  "rto": {"met": 14, "with_objective": 16, ...}, "rpo": {...}, "recovery_measurements": 16,
#  "findings": {"open": {"critical": 2, "high": 1, "medium": 0, "low": 0}, "opened_in_window": 5}}
```

| Field | Counts |
| --- | --- |
| `validation` | Rule checks that passed, over those that passed, failed or errored (skipped left out), in runs that finished in the window. Rule checks, not rows |
| `issues` | Failing or errored rule checks by rule, most frequent first (top 10; `total` covers every rule) |
| `runs`, `refused` | Runs that finished in the window, by final state; refused means stopped in preflight, over runs finished |
| `chaos`, `scenarios` | Scenario steps that passed, over scenario steps that finished or errored; and each scenario's latest step (up to 10) with its RTO and RPO result |
| `rto`, `rpo` | The same attainment as `/v1/assurance/recovery`, by fault onset |
| `findings` | Open findings now by severity, whatever the window; and findings opened in the window |

- **Window:** `[since, until)`, the last 30 days by default, at most 366 days; runs and steps count by when they finished.
- **API keys:** a key with `runs:read` sees its own project only.

### The worker

```bash
export RAI_WORKER_DATABASE_URL=postgresql://rai_worker:...@127.0.0.1:55431/rai
export RAI_WORKER_CONNECTORS=./worker-connectors     # *.yaml connector configs
uv run rai worker            # or: rai worker --once
```

- **One step at a time.** The worker claims a queued run, renews its lease between steps and executes each plan item as one step.
- **Steps are atomic.** A step's observations are committed together with the step. If the worker dies, another worker resumes the run after the lease (5 minutes) runs out: finished steps are never repeated, so observations are never duplicated.
- **The worker refuses to start** unless it logs in as `rai_worker`.

**A `contract_rule` test case** names a connector configured on the worker, never a path:

```json
{"name": "txn.contract", "type": "contract_rule", "target_asset_id": "...",
 "definition": {"connector": "feed", "environment": "SANDBOX-01",
                "references": {"accounts": "accounts.csv", "merchants": "merchants.csv"},
                "fail_on": "critical", "freshness_reference": "newest_row",
                "upstream": {"ledger_postings": "ledger"}}}
```

- **The asset's `locator`** is read through that connector (a file under its root, or a table in a read-only session).
- **`freshness_reference`:** `newest_row` (default) matches `rai validate` without `--as-of`; `now` measures against the run's clock, for live feeds.
- **`upstream`** (optional) maps upstream asset names to worker connectors, so their data is read and digested for lineage.
- **Cross-batch duplicates** are checked against a key history kept per tenant in `RAI_WORKER_STATE`.
- **`environment`** must be a registered, active environment (see [Environments and targets](#environments-and-targets)); otherwise the run is refused.
- **Not yet supported:** recovery assertions and TEVV metrics. A plan containing them is refused with a reason, never passed silently.

**A chaos scenario item** needs a worker set up for faults: `RAI_WORKER_SIGNING_KEY` and `RAI_WORKER_CHAOS_AGENT_URL` (see the [chaos agent](chaos-agent.md)). Otherwise the run is refused.

- **Preflight.** The worker checks again that the scenario's latest review is an approval. It also checks every fault against the environment's allow-list and ceiling. Any problem refuses the run, and nothing reaches the agent.
- **Each fault in order:** signed and injected, with its onset recorded (`fault.onset`). The worker then holds the fault for its duration, renewing its lease, and asks the agent to revert it (`fault.reverted`). Faults use the step keys `item-N.fault-1`, `item-N.fault-2` and so on.
- **A fault the agent refuses, or that fails,** ends the step in `error` with the reason. Later faults are not tried, and the run fails.
- **A worker that dies mid-scenario** leaves the open fault to the agent's expiry and the watchdog. The retry gets the agent's record of each fault it already injected, waits out what remains, and records nothing twice.

**Measuring recovery (FR-08.1, FR-08.2).** A scenario can add a `recovery` plan:

```json
"recovery": {"probes": ["payments-health"], "ledger": "payments-ledger",
             "rto_seconds": 120, "rpo_seconds": 0, "window_seconds": 300, "stable_checks": 3}
```

- **Probes and the ledger are allow-listed targets.** The worker uses each target's registered `health_url`; a scenario names targets, never addresses. A target that isn't allow-listed with a `health_url` refuses the run.
- **RTO** runs from the first fault's onset to *validated restoration*. After the last fault is reverted, every probe has to answer 2xx `stable_checks` times in a row, and the time of the first of those checks is the restoration time. A recovery that flaps counts only once it holds. If nothing is restored within `window_seconds`, the step fails.
- **RPO** comes from the ledger head (`seq`, `committed_at`, `rows`), read before the first fault and again after restoration. The "before" head is kept as an observation, so a retry measures from the same point. If no committed row was lost, RPO is 0 s. If the ledger went back, RPO is the time from the last surviving record to the onset. Lost rows always fail the step.
- **Observations:** `recovery.rto`, `recovery.rpo` and `recovery.ledger_before`, on the scenario's step. A missed objective makes the step's verdict `fail` with the reason; the step itself is still `done`, because the measurement worked.

**Consistency after recovery (FR-08.3).** Healthy is not the same as correct. A `consistency_check` test case reconciles its target asset against a source it is fed from, so a plan of *scenario, then check* shows whether the system came back with nothing lost or doubled:

```json
{"name": "ledger.consistency", "type": "consistency_check", "target_asset_id": "<fact_transaction>",
 "definition": {"connector": "warehouse", "environment": "SANDBOX-01",
                "source": {"asset": "ledger_transactions", "connector": "ledger",
                           "where": {"status": "posted"}, "rename": {"seq": "ledger_seq"}},
                "key": ["txn_id"], "compare": ["amount", "ledger_seq"],
                "within_seconds": 300, "interval_seconds": 10}}
```

- **The source must be declared upstream of the target** (see [Lineage](#lineage)), and both are read through connectors configured on the worker. A definition names assets and connectors, never locations. Otherwise the run is refused.
- **`where` and `rename`** shape the source the way the target holds it. Here the warehouse holds only posted rows, and calls `seq` `ledger_seq`. A `where` at the top level filters the target.
- **The source keeps growing while the target catches up.** So the worker snapshots the source when the check starts, then reads the target and the source again every `interval_seconds` until all four findings are empty, or `within_seconds` has passed:
  - `consistency.missing`: snapshot keys that are not in the target (lost);
  - `consistency.duplicated`: keys the target holds more than once (doubled);
  - `consistency.unexpected`: target keys the source does not have;
  - `consistency.mismatched`: keys whose `compare` columns differ. Rows written after the snapshot don't have to have arrived yet, but they must match once they have.
- **Each finding is an observation** with its count, up to 10 sample keys, the rounds and the seconds taken. Any finding left at the end makes the step's verdict `fail`. Lineage records the hop with both frames' digests.
- **Values are compared as the connectors return them (text),** so both sides must render a column the same way (a `numeric(14,2)` on each side does).
- **`allow_unexpected: true`** still counts target rows the source never had, but records them as `skipped` instead of failing. Use it when the target has other writers too.

**RPO ground truth (B-30).** The ledger-based RPO above relies on the system under test reporting its own ledger head honestly. For ground truth, run SandboxBank's seeder during the scenario (see [SandboxBank](../sandboxbank/README.md#seeding-log-b-30-rpo-ground-truth)) and add a check that reconciles the ledger against the seeder's log:

```json
{"name": "ledger.rpo_truth", "type": "consistency_check", "target_asset_id": "<ledger_transactions>",
 "definition": {"connector": "ledger", "environment": "SANDBOX-01",
                "source": {"asset": "seeding_log", "connector": "seeder", "where": {"acked": "true"}},
                "key": ["txn_id"], "compare": ["seq"], "allow_unexpected": true,
                "rpo": {"at": "acked_at", "objective_seconds": 0}}}
```

- **Declare the seeding log upstream of the ledger** (the seeder feeds it), and give the worker a connector to the log's folder.
- **`recovery.rpo_truth`** is the run's first fault onset minus the earliest *acknowledged* write that the ledger no longer holds. It's 0 s if nothing was lost, and never negative. Without a fault in the run, a loss can't be dated (`seconds: null`).
- **The step fails** if any acknowledged write was lost, if the RPO exceeds `objective_seconds`, or if the ledger's own account in the same run (`recovery.rpo`) disagrees about whether anything was lost. The observation shows both (`measured`), so a system that reports a healthy ledger head while dropping writes is caught.

**Drift checks (FR-09.2).** A `drift_check` test case compares columns of its target asset (the *current* data: this week's batch, or a model's scores) with a *reference* asset of the same project (the batch it was approved on), both read through worker connectors:

```json
{"name": "amount.drift", "type": "drift_check", "target_asset_id": "<this week's batch>",
 "definition": {"connector": "feed", "environment": "SANDBOX-01",
                "reference": {"asset": "batch_0", "connector": "feed"},
                "columns": [{"name": "amount"},
                            {"name": "channel", "kind": "categorical", "psi_alert": 0.1}],
                "bins": 10, "min_rows": 100}}
```

- **PSI** (population stability index) for every column: numeric columns are binned at the reference's quantiles (deciles by default; fewer bins when the column has few distinct values), categorical columns by category. Below 0.1 is stable, 0.1 to 0.25 a moderate shift, above 0.25 a significant one; `psi_alert` (default 0.25) is where the column fails.
- **KS** for numeric columns: the two-sample Kolmogorov-Smirnov statistic `D`, the largest gap between the two distributions (0 identical, 1 disjoint), with its asymptotic p-value. The alert is on `D` (`ks_alert`, default 0.1; `null` for PSI alone), because with 100,000 rows almost any difference has a tiny p-value; the p-value is reported, not judged.
- **Sample sizes always.** Each column is an observation `drift.<column>` (kind `metric`) with PSI, its band, `D`, the p-value, both row counts, the share of nulls on each side (nulls are left out), and the bins with their shares. Fewer than `min_rows` values on either side is an error, not a pass.
- **Pinned.** The reference asset is pinned with the run, like the target (`pins.items[].reference`); an unknown name refuses the run (`422 runs.drift_reference_unknown`). Lineage records both datasets with the digest of what was read.
- **Verdict.** Any alert, or a column that cannot be judged, fails the step, and so the run, like any test. To watch drift without gating a release, keep drift checks in a plan of their own.

**Model evaluation (FR-09.1).** A `tevv_metric` test case judges a binary classifier from its target asset, a table of one score and one label per row, read through a worker connector. SandboxBank's `sandboxbank model score` writes exactly that (`txn_id, score, is_fraud`):

```json
{"name": "fraud.model", "type": "tevv_metric", "target_asset_id": "<a batch's scores>",
 "definition": {"connector": "feed", "environment": "SANDBOX-01",
                "score_column": "score", "label_column": "is_fraud", "threshold": 0.5,
                "metrics": [{"name": "recall", "at_least": 0.4},
                            {"name": "roc_auc", "at_least": 0.95},
                            {"name": "false_positive_rate", "at_most": 0.001}],
                "judge_on": "estimate", "confidence": 0.95,
                "min_rows": 100, "min_positives": 30, "min_negatives": 30}}
```

- **Metrics.** `accuracy`, `precision`, `recall`, `specificity`, `false_positive_rate`, `f1` and `roc_auc`, each with a target `at_least`, `at_most` or both. A row is flagged when `score >= threshold`; ROC AUC uses every score, with ties counted as half.
- **No number without its n.** Each metric is an observation `tevv.<metric>` (kind `metric`) with its value, its numerator and denominator (recall is true positives *of all positives*), and a confidence interval: Wilson's score interval for the proportions, Hanley and McNeil's approximation for ROC AUC, none for F1. `tevv.sample` records the confusion matrix, the rows read and the rows left out.
- **Too little data is an error, not a pass.** Fewer than `min_rows` usable rows, `min_positives` positives or `min_negatives` negatives, and nothing is judged: the metrics are still recorded, with outcome `error`. A metric whose denominator is 0 (precision when nothing was flagged) is undefined, also an error, never 0.
- **Judging.** `judge_on: estimate` compares the metric itself with its target. `confidence_bound` is stricter: `at_least` must be met by the interval's lower end, `at_most` by its upper end, so a pass means the data supports the target at the stated `confidence`. F1 can only be judged on the estimate.
- **Rows.** A null score or label leaves the row out and is counted (`null_rows`). A score that is not a finite number, or a label other than true/false or 1/0, is an error: the data is not what the test says it is.
- **Verdict and lineage.** A missed target, a metric that cannot be judged, or too little data fails the step, and so the run. Lineage records the digest of the scores that were judged.

**Canary privacy checks (FR-05.1).** A `privacy_canary` test case searches its target asset (a warehouse table, an export) for canary values: fake personal details planted in a source system's table that nothing downstream should copy. The values come from an answer key, another asset of the project with the columns `canary, kind, value`; SandboxBank's `sandboxbank canaries write` writes one as `needles.csv`. Both are read through worker connectors:

```json
{"name": "warehouse.no-pii", "type": "privacy_canary", "target_asset_id": "<dim_account>",
 "definition": {"connector": "warehouse", "environment": "SANDBOX-01",
                "key": {"asset": "canary-key", "connector": "feed"},
                "columns": null, "case_sensitive": false}}
```

- **Search.** Every text column (or the `columns` listed) is searched for every value at once, as a substring and ignoring case by default, so an email inside a note or upper-cased by a transform still counts. Overlapping matches are reported once, as the longest: a leaked email is an email, not also the marker it contains.
- **Never repeated in full.** The observation `privacy.canary` (kind `rule_result`) holds the hit count, the rows with a hit (`failing_count`), counts by kind, column and canary, and up to 50 hits, each with its column, 0-based row, canary and kind, and the value masked (`av…id, 41 chars`). Nothing RAI records or returns holds a canary value in full.
- **A key that cannot work is an error.** No values, a value shorter than 6 characters (it would match by chance), a value listed twice, or a listed column missing from the target: the step is an error, never a pass.
- **Pinned.** The key is pinned with the run (`pins.items[].reference`); an unknown name refuses the run (`422 runs.canary_key_unknown`). Lineage records the digests of the target and the key.
- **Verdict.** Any hit fails the step, and so the run.

**Canary sweeps of logs and evidence bundles (FR-05.1).** What a run leaves behind is searched offline, with the same key and the same search, by `rai canary scan`. It needs no API or database:

```bash
uv run rai canary scan --key data/canaries/needles.csv serve.log worker.log bundles/
docker compose logs --no-color | uv run rai canary scan --key data/canaries/needles.csv -
# FAIL    worker.log line 812: email of CUS-0000003 (av…id, 41 chars)
# FOUND   1 canary value(s) of 1 canary(ies) found in 1 source(s); ...
```

- **Sources.** Text files and logs are searched line by line (`line 12`), `.gz` files decompressed. In `.json` files and in the members of a `.zip` (an evidence bundle, `bundle.zip!rai-evidence-<run>/steps.json`), every key, string and number is searched and a hit is placed by its JSON path (`$[0].error`), so a card number stored as a number still counts. A folder is searched recursively; `-` reads standard input. The key is `needles.csv` or SandboxBank's `canaries.json`, and is skipped if it is among the sources.
- **Masked.** Like the test type, a hit names its source, place, canary and kind, never the value in full. `--json` prints the whole result.
- **Fail closed.** Exit 0 no canary value, 1 a value was found, 2 the key cannot be used or a source could not be read.
- **In CI.** The gate job runs a plan whose canary check catches a planted leak, then sweeps RAI's API and worker logs and the evidence bundles of all its runs: they must hold no canary value, while the same sweep must find the leak in the export itself. The chaos job plants canaries in ledger-db, runs the whole fault suite, and sweeps every sandbox service's logs.

## Findings

A finding records a defect a run observed (FR-10.1). It cites the failing or errored observation it came from (run, step, rule), and has a severity, an owner and, once someone works on it, a fix reference.

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `POST /v1/projects/{id}/findings` | engineer, reviewer, admin | `{"run_id", "step_key", "rule", "title", "severity", "description"?, "owner_id"?}`; the owner defaults to you |
| `GET /v1/projects/{id}/findings` | | Oldest first; `?status=open\|closed\|dismissed`, `?severity=critical\|high\|medium\|low` |
| `GET /v1/findings/{id}` | | |
| `PATCH /v1/findings/{id}` | engineer, reviewer, admin | Owner (a member), `fix_ref`, description; open findings only |
| `POST /v1/findings/{id}/retest` | engineer, reviewer, admin | `{"run_id"}`: closes the finding if the run passes the rule |
| `POST /v1/findings/{id}/dismiss` | reviewer, admin | `{"reason"}`; not by the finding's creator |

```bash
curl -s -X POST "$API/v1/projects/$PROJECT/findings" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"run_id": "'$RUN'", "step_key": "item-1", "rule": "unique_across_batches:txn_id",
       "title": "Duplicate txn_id in the feed", "severity": "critical"}'
# 201 {"finding_id": "...", "status": "open", "severity": "critical", "owner_id": "...", ...}
curl -s -X POST "$API/v1/findings/$FINDING/retest" -H "$AUTH" -d '{"run_id": "'$LATER_RUN'"}'
# 200 {"status": "closed", "retest_run_id": "...", ...}   or 409 findings.retest_not_passing
```

- **One finding per observation.** Citing a passing observation is `422 findings.source_not_failing`. Citing an observation that already has a finding is `409 findings.duplicate`, with a `Location` header pointing to the existing one.
- **Only a passing retest closes a finding.** The retest run must be:
  - in the same project;
  - queued after the finding was opened (otherwise `409 findings.retest_too_early`);
  - finished, passed or failed (otherwise `409 findings.retest_unfinished`);
  - observing the same rule for the same test, with outcome `pass` and no failure of it (otherwise `409 findings.retest_not_passing`).
- **Dismissal** ends a finding that is not a defect. It needs a reason and someone other than the creator (`403 findings.self_dismissal`).
- **Resolved findings never change** (`409 findings.resolved`). Title, severity and source never change either, so a critical finding cannot be quietly downgraded.
- **Enforced in the database too.** A trigger enforces the same rules, deletes are refused, and every change is an audit entry (`finding.insert`, `finding.update`).

## Waivers and the release gate

A project's **release gate** is blocked while any open critical finding has no waiver in force (FR-12.2). A waiver lets a team ship knowingly with a known defect, for a limited time, with a second person's name on it (FR-12.1).

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `GET /v1/projects/{id}/gate` | | `pass` or `blocked`, with the blocking and the waived findings; also with an API key that has `runs:read` |
| `POST /v1/findings/{id}/waivers` | engineer, reviewer, admin | `{"reason", "expires_at"}`: an open critical finding only, expiring at most 90 days ahead |
| `GET /v1/findings/{id}/waivers` | | Every waiver asked for, oldest first, with its decisions |
| `GET /v1/waivers/{id}` | | |
| `POST /v1/waivers/{id}/decisions` | reviewer, admin | `{"decision": "approve"\|"reject", "comment"?}`; not by the requester or the finding's owner |

```bash
curl -s "$API/v1/projects/$PROJECT/gate" -H "$AUTH"
# {"verdict": "blocked", "blocking_count": 1, "waived_count": 0,
#  "blocking": [{"finding_id": "...", "title": "Duplicate txn_id in the feed", ...}], ...}
curl -s -X POST "$API/v1/findings/$FINDING/waivers" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"reason": "fix ships in 2.4; duplicates are filtered downstream", "expires_at": "2026-12-01T00:00:00Z"}'
# 201 {"waiver_id": "...", "status": "pending", "in_force": false, ...}
curl -s -X POST "$API/v1/waivers/$WAIVER/decisions" -H "$REVIEWER_AUTH" -d '{"decision": "approve"}'
# 201 {"status": "approved", "in_force": true, ...}   then the gate is "pass" with the finding under "waived"
```

- **What blocks.** Open findings of severity `critical`. High, medium and low findings, closed findings and dismissed findings do not block, and cannot be waived (`422 waivers.not_blocking`, `409 findings.resolved`).
- **A waiver is in force** while its latest decision is `approve`, it has not expired and its finding is open. Decisions are append-only; a later `reject` withdraws an approval. A pending or rejected waiver lifts nothing.
- **Expiry is required**, in the future and at most 90 days ahead, with a time zone (`422 waivers.expiry_out_of_range`, `waivers.expiry_without_zone`). When it passes, the gate blocks again without anyone acting; an expired waiver cannot be approved (`409 waivers.expired`), and a new one may be asked for.
- **One at a time.** A finding with a pending waiver, or one in force, refuses another (`409 waivers.exists`, with a `Location` header).
- **No self-approval.** The requester and the finding's owner cannot decide (`403 waivers.self_approval`). A trigger enforces this in the database too, both tables are never changed or deleted, and every row is an audit entry (`waiver.insert`, `waiver_decision.insert`).

### Release decisions

The gate says whether a project *may* ship. A **release decision** records that a named reviewer decided it *should*, for one labelled release such as `v1.4` (B-42: FR-12.1, FR-12.2).

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `POST /v1/projects/{id}/releases/{label}/decisions` | reviewer, admin | `{"decision": "approve"\|"reject", "justification"}`; users only, never an API key |
| `GET /v1/projects/{id}/releases` | | Each release with its latest decision, most recent first; also with a `runs:read` key |
| `GET /v1/projects/{id}/releases/{label}` | | Its status and every decision, oldest first |

```bash
curl -s -X POST "$API/v1/projects/$PROJECT/releases/v1.4/decisions" -H "$REVIEWER_AUTH" \
  -H 'Content-Type: application/json' -d '{"decision": "approve", "justification": "F-031 waived until 2.4"}'
# 201 {"status": "approved", "latest": {"gate": {"verdict": "pass", "blocking_findings": [],
#      "waived_findings": ["..."], "waivers": ["..."]}, ...}, "decisions": [...]}
```

- **Labels** are 1–64 characters: letters, digits and `. _ + -`, starting with a letter or digit.
- **The gate is recorded with every decision**: its verdict, the findings blocking it and the waived findings with the waivers in force. The database computes this when the row is written and ignores any values a caller sends.
- **No approval while the gate is blocked** (`409 releases.gate_blocked`). Rejecting is always possible.
- **No approving your own exception.** An approval that relies on a waiver is refused from that waiver's requester and from the waived finding's owner (`403 releases.self_approval`). They may still reject.
- **Append-only.** For a release, the latest decision counts: a later `reject` withdraws an approval. Decisions are never changed or deleted, and each is an audit entry (`release_decision.insert`).

## Lineage

Lineage records **which data a run actually read**, as SHA-256 digests (FR-04.1).

**1. Declare how the pipeline fits together.** Each declaration says "this asset is derived from that one", optionally through a named transform:

```bash
curl -s -X POST localhost:8000/v1/assets/$TXN_TABLE/inputs -H "authorization: Bearer $ACCESS"   -H 'content-type: application/json'   -d '{"input_asset_id": "'$ETL_JOB'", "transform_ref": "etl-job@4f2a9c1"}'
```

- Both assets must be in the same project (`422 lineage.other_project`).
- A declaration that would close a cycle is refused (`422 lineage.cycle`); a repeated one is `409`.
- Asset kinds follow the chain `source -> transform -> table -> feature_set -> model`; a transform usually has no locator.

**2. Run.** When a run is queued it pins every hop upstream of each target (at most 100 per plan item). The worker then records, in the same transaction as the step's observations:

- every pinned hop, with the digest of each end it read;
- the target into the run;
- each reference dataset into the run (`from_name` `reference:accounts`).

An upstream asset is read and digested only if the test definition maps it to a connector (`upstream`). Otherwise it still appears as a hop, with a null digest. A step that could not read its data records no lineage.

**3. Read the graph:**

```bash
curl -s localhost:8000/v1/assurance/runs/$RUN/lineage -H "authorization: Bearer $ACCESS"
# {"digest_format": "rai-frame-digest/1", "assets": [...],
#  "edges": [{"step_key": "item-1", "from_name": "txn_table", "from_digest": "e03e85e9...",
#             "from_rows": 2002, "to_asset_id": "...", "to_digest": "0d8dd300...",
#             "transform_ref": "features@9b3e", ...}, ...]}
```

`to_asset_id: null` means the run itself.

**The digest (`rai-frame-digest/1`)** is the SHA-256 of the batch **after** the connector has normalised it to text:

- The same data has the same digest from CSV, Parquet or PostgreSQL.
- It ignores row and column order, because a PostgreSQL read has no defined order.
- It counts duplicate rows and tells a null from an empty string.

`rai validate` reports the same value as `batch.source.content_sha256`, so a run's digests can be checked locally. The exact encoding is documented in `src/rai/lineage/digest.py`, and a test pins it.

## API keys and the CI gate

CI signs in with a **project API key**, not a user account (FR-06.1, threat S-02).

| Endpoint | Roles | Notes |
| --- | --- | --- |
| `POST /v1/api-keys` | admin, engineer | `{"project_id": ..., "name": "github-actions", "expires_in_days": 30}`; the key is in this response only |
| `GET /v1/api-keys` | admin, engineer | `?project_id=`; prefix, scopes, state; never the secret |
| `GET, DELETE /v1/api-keys/{id}` | admin, engineer | `DELETE` revokes at once |

- **Format:** `Authorization: Bearer rai_<prefix>_<secret>`. Only the SHA-256 of the secret is stored; the prefix identifies the key in lists and the audit log.
- **Expiry is required:** 1 to 90 days (default 30). Revocation is the only change a key ever takes; keys are never deleted, and both events are audited.
- **Scopes:** `runs:start` and `runs:read` (both by default). A key starts and reads runs of **its own project only**; another project's runs are `404`, and every other endpoint is `403` (`auth.key_scope`, or `auth.key_not_allowed` for account endpoints such as `/v1/me`).
- **A key never outlives its creator's access.** It stops working when the user who created it is removed, disabled or no longer an admin or engineer, and when its project is archived.
- **Accountability:** a run started by a key records the key (`api_key_id`) and its creator (`triggered_by`); the audit log names the key as actor.
- **Rate limit:** 120 requests per minute per key (`429` with `Retry-After`).

**`rai run`** starts a plan and gates on its verdict and on the project's release gate:

```bash
export RAI_API_URL=https://rai.example.test RAI_API_KEY=rai_...   # never on the command line
uv run rai run --plan ci-contracts          # a plan name runs its newest version
# run 0193... queued (plan ci-contracts @ 4f2a9c1)
# FAILED run 0193...: item-1 pass, item-2 fail
#   FAIL  item-2 unique_across_batches:txn_id (critical) 2/2,002 rows ...
# GATE blocked: 1 open critical finding(s) without a waiver in force
#   BLOCKS 0193... Duplicate txn_id in the feed
```

- **Exit codes:** `0` the run and the release gate passed, `1` the run failed or the release gate is blocked, `2` no verdict (refused, error, timeout, unknown plan, bad key, API unreachable), the same as `rai validate`.
- `--trigger-ref` defaults to `GITHUB_SHA`; `--idempotency-key` makes a retried request return the same run; `--no-wait` only queues; `--no-release-gate` skips the release gate. A release gate that cannot be read (for example a key without `runs:read`) is exit `2`, never a pass.
- Plain `http://` is refused except to localhost, so a key never crosses a network in clear.

**The GitHub Action** (`action.yml` at the repository root) wraps `rai run`. It installs RAI from this repository's lockfile and writes a job summary:

```yaml
- uses: resilinet-ai/resilinet-ai@<commit SHA>   # pin by SHA
  with:
    api-url: https://rai.example.test
    api-key: ${{ secrets.RAI_API_KEY }}
    plan: ci-contracts
```

Make the job a **required status check** on the branch, and a failing contract blocks the pull request. Outputs: `run-id`, `state`, `verdict`, `gate` (`pass` or `blocked`). This repository's CI job "CI gate" runs the action against a live API and worker on every pull request: a clean plan must pass, and a plan whose batch re-sends rows must fail.

## Evidence

`GET /v1/assurance/runs/{id}/evidence` exports a finished run's **evidence bundle**: a ZIP file to keep, or to hand to someone who does not use RAI (FR-13.1). Any member can export it, and so can an API key with `runs:read`, for its own project.

```bash
curl -s -OJ "$API/v1/assurance/runs/$RUN/evidence" -H "$AUTH"
# rai-evidence-<run_id>.zip      X-RAI-Manifest-SHA256: 3e7a...
```

| Member (under `rai-evidence-<run_id>/`) | Contents |
| --- | --- |
| `bundle.json` | Format `rai-evidence/1`, the scope (tenant, project, run), counts, what is excluded, the audit-chain **anchor**, and the tamper-evidence statement |
| `run.json` | The run, with the pins it executed from (plan version, definitions, environment) |
| `steps.json`, `observations.json` | Every step and every observation, with the values observed and expected |
| `lineage.json` | Every lineage edge, with the SHA-256 digest of the data the worker read |
| `recovery.json` | Recovery measurements (B-29), for chaos runs |
| `approvals.json` | The scenario approvals the run was pinned to: who approved, when |
| `audit.json` | The run's own audit-chain entries, with `prev_hash` and `record_hash` |
| `manifest.json` | The SHA-256 and size of every member above |
| `signature.json` | An Ed25519 signature over `manifest.json`, with the key id and public key, when the server has a signing key (B-35). The only member the manifest does not list |

- **Deterministic.** Members are canonical JSON (sorted keys, no spaces, UTF-8, times in UTC with microseconds), stored uncompressed in a fixed order with a fixed date and permissions. Two exports of the same run are byte-identical, on any platform and at any time.
- **Only a finished run** (`passed`, `failed`, `refused`, `aborted` or `error`) has a bundle; earlier it is `409 evidence.run_unfinished`. A finished run's results never change.
- **Anchor.** `bundle.json` records the `seq` and `record_hash` of the run's last audit entry. If the audit chain is later rewritten at or before that entry, the live hash no longer matches the anchor (threat T-02). The anchor is the run's own entry, not the chain's head, because the head moves with every change and would make each export different.
- **Not included:** synthetic data (only its digests) and secret values (pins hold secret references only).
- **Each export is audited** (`evidence.export`), with who took it and the manifest's SHA-256, which is also the `X-RAI-Manifest-SHA256` header.
- **Tamper-evident, not tamper-proof** (NFR-AUD-2). The manifest and its signature show whether a member changed after export; they do not show that the recorded facts were true.

**Signing and `rai verify`** (FR-13.2). The operator makes a key once and gives the API a secret reference to it:

```bash
uv run rai admin evidence-keygen --out /run/secrets/rai_evidence_signing_key
# stderr: private key written ... (key id 1f3a...); set RAI_EVIDENCE_SIGNING_KEY=file:...
# stdout: the public key, e.g. Zb3k...=   publish it where verifiers can trust it
```

With `RAI_EVIDENCE_SIGNING_KEY` set, every bundle carries `signature.json`, the export names the key in `X-RAI-Signature-Key-Id` and in its audit entry, and `GET /v1/evidence/public-key` returns `{"algorithm": "ed25519", "key_id", "public_key"}` (`404 evidence.unsigned` without a key). The signature is over a fixed domain string followed by `manifest.json`'s bytes, and Ed25519 is deterministic, so a signed bundle is still byte-identical on every export.

Anyone can check a bundle offline, without RAI:

```bash
uv run rai verify rai-evidence-0193....zip --key Zb3k...=      # or --key @evidence.pub
# OK      manifest: every member matches (sha256 3e7a...)
# OK      signature: signed by trusted key 1f3a...
# INFO    anchor: audit entry seq 1204, record_hash 8d2c...; compare with the live chain
# VERIFIED rai-evidence-0193....zip
```

- **Exit codes:** `0` verified; `1` a member changed, was added or is missing, or the signature is missing, invalid or by a key you did not pass; `2` the bundle or a key could not be read.
- **Pin the key.** A valid signature by a key you did not pass with `--key` fails: anyone who edits a bundle can re-sign it with their own key. Take the public key from a source you trust (your release notes, a config repository), not from the bundle, and not only from the server whose bundles you check.
- **Unsigned bundles** fail unless you pass `--allow-unsigned`; the manifest is still checked.
- **The anchor** is printed, not checked: `rai verify` works offline. Compare it with the live chain (`rai audit verify`, or the entry at that `seq`).

## Errors

Every error is RFC 9457 problem details (`application/problem+json`) with a stable `code`, for example `auth.invalid_credentials`, `tenant.not_found` or `members.last_admin`. Validation errors list each problem in `errors`. Request bodies over 1 MiB are refused with `413`.

## Audit

Every change to tenants and memberships is written to the tenant's hash-chained audit log by a database trigger, with the signed-in user as the actor. Check a chain with `rai audit verify --tenant <id>`.
