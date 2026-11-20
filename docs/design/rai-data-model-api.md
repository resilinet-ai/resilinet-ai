# Resilinet-AI (RAI): Data Model & API Contract

| | |
| --- | --- |
| **Version** | v0.2 (reconstructed draft), 2026-11-17 |
| **Status** | Draft. **Pending review and approval by the author of record**, Trevor Kauyu (ADR-007) |
| **Baseline** | This document, as built (decision DESIGN-V03). The 2026-11-13 v0.1 drafts, found on 2026-11-20, are not adopted |

> **How this document was made.** Reconstructed on 2026-11-17 by an AI assistant (Claude Code) from the migrations (`src/rai/migrations/versions/0001`–`0009`), the API routes and `docs/api.md`, because the v0.1 PDF was never exported. Section numbers follow the ones the code cites ("Data Model section 3" and so on). It describes the schema and API **as built**; where the build departed from v0.1, the decision log records why. The operational reference for API callers is `docs/api.md`.

## 0. Conventions

- **Identifiers** are UUIDv7 (time-ordered), generated in the database.
- **Versions are immutable.** Contracts, test cases, plans and scenarios are published as numbered versions (`name@version`). A new version is a new row; a trigger refuses edits to a published version.
- **Runs pin versions.** A run records exactly which versions, with their SHA-256, it used, so a result can always be traced and replayed.
- **Corrections are new rows.** Results, approvals, lineage and audit entries are insert-only.
- **Every tenant row carries `tenant_id`**, and foreign keys include it, so a row can never point into another tenant.

## 1. Entities

```
tenants ─┬─ users ── memberships (role)
         ├─ audit_chain (hash-chained)
         ├─ environments ── environment_targets (allow-list)
         └─ assurance_projects ─┬─ pipeline_assets ─┬─ data_contracts (versions)
                                │                   └─ asset_inputs (declared lineage)
                                ├─ test_cases (versions)
                                ├─ chaos_scenarios (versions) ── scenario_approvals
                                ├─ test_plans (versions) ── plan_items (test case | scenario)
                                ├─ api_keys
                                └─ runs ─┬─ jobs (queue)
                                         ├─ run_steps
                                         ├─ observations
                                         └─ lineage_edges
```

## 2. Data contracts: identity

A contract version's identity is the **SHA-256 of its canonical JSON** form. Runs pin that hash; a contract whose stored hash no longer matches its pin is refused. Rule ids are stable (`amount.range`, `txn_id.unique`), so results can be compared across versions. A JSON Schema for the contract format is committed and tested for drift. The YAML format is in section 9.

## 3. Definitions

| Table | Holds | Mutability |
| --- | --- | --- |
| `assurance_projects` | name, owner, status (`active`, `archived`), `data_classification` (`synthetic`, `authorized_snapshot`), default environment | The one mutable definition table (ETag/If-Match) |
| `pipeline_assets` | kind (`source`, `table`, `transform`, `feature_set`, `model`), name, locator | Insert; locator set by engineers |
| `data_contracts` | asset, version, spec (JSON), `spec_sha256` | Immutable versions |
| `asset_inputs` | asset ← input asset, optional `transform_ref` | Insert/delete; a trigger refuses cycles and cross-project links |
| `test_cases` | type (`contract_rule`, `recovery_assertion`, `consistency_check`, `tevv_metric`, `drift_check`, `privacy_canary`), target asset, definition (≤ 64 KiB JSON), severity, expected result | Immutable versions |
| `test_plans`, `plan_items` | ordered items: a test case (optionally with a pinned contract) or a scenario; optional environment; trigger (`manual`, `ci`, `schedule`) | Immutable versions |

## 4. Runs, jobs, observations and lineage

| Table | Holds |
| --- | --- |
| `runs` | plan version, **pins** (JSON: every input with its hash), state, verdict (`pass`, `fail`), reason, trigger ref (commit SHA), idempotency key |
| `jobs` | the queue: run, lease owner and expiry, attempts (cap 3) |
| `run_steps` | one row per plan item: state (`done`, `error`), verdict, error, attempt; primary key (run, step) so a step is recorded once |
| `observations` | kind (`rule_result`, `metric`, `refused`, `note`), rule, outcome (`pass`, `fail`, `error`, `skipped`), observed value, expected, row and failing counts; unique per (run, step, rule) |
| `findings` | a defect a run observed (FR-10.1): project, title, severity (`critical`, `high`, `medium`, `low`), status (`open`, `closed`, `dismissed`), the source observation (run, step, rule; one finding each), owner, fix reference, retest run, resolution; title, severity and source never change; closed only by a later run that passes the rule; never deleted; audited |
| `waivers` | a request to waive an open critical finding (FR-12.2): finding, reason, expiry (at most 90 days), requester; insert-only; audited |
| `release_decisions` | a named reviewer's approve or reject of a labelled release (B-42), with a justification and the gate as it stood: verdict, blocking findings, waived findings and their waivers, all computed by a trigger; approval refused while blocked or by anyone whose waiver or finding it relies on; append-only, the latest per release counts; audited |
| `waiver_decisions` | a named reviewer's approve or reject of a waiver (FR-12.1); append-only, the latest counts; never by the requester or the finding's owner (trigger); audited |
| `lineage_edges` | step, from asset (or a named reference dataset), to asset (or the run), SHA-256 of each side's frame (`rai-frame-digest/1`), rows, transform ref |
| `recovery_measurements` | one per scenario step that measured recovery (B-29): scenario, method, onset, revert and restoration times, RTO and RPO with their objectives and whether each was met, ledger rows lost; unique per (run, step), foreign key to its `run_steps` row, insert-only |

`lineage_edges` gained `step_key`, `from_name` and `from_rows` relative to v0.1, so a reference dataset can be an input and each edge names the step that read it.

## 5. Environments, targets and scenarios

- **`environments`**: name, `env_type` **`SANDBOX` or `STAGING` only**, status (`active`, `retired`), `max_fault_duration` (default 15 min, at most 60), `production_flag` (a check holds it false). Names containing `PROD` are refused; name and type never change. Production cannot be represented, so no API bug can register it.
- **`environment_targets`**: the allow-list. Kind (`container`, `proxy`, `db`, `http`), name, `required_label` (the label the target must carry), optional `health_url` for recovery probes, and for a container its `image_digest` (B-45; required for new container targets, NULL for those registered before). Admin only.
- **`chaos_scenarios`**: immutable versions with a blast radius (`text[]`), ordered faults (JSON, the seven types) and an optional recovery plan (probes, ledger, RTO/RPO objectives, window, stable checks).
- **`scenario_approvals`**: append-only decisions (`approve`, `reject`); the latest counts. A trigger refuses a reviewer who is the scenario's author. An approval has an `expires_at` at most 90 days after it was given (B-43); one without (given before B-43) counts as expired.

## 6. Tenancy and database-enforced rules

- **Row-level security** on every tenant table, keyed on the transaction-local setting `app.tenant_id`. With no tenant set, no rows are visible: it fails closed. A CI test fails if any `rai` table has RLS disabled.
- **Roles:** the application runs as `rai_app` (NOLOGIN, NOBYPASSRLS, cannot create tenants or change audit entries) via `SET LOCAL ROLE`; the worker as `rai_worker`; migrations as the owner.
- **Audit chain:** `audit_chain` rows hold the previous hash, the record hash and a sequence number assigned inside the trigger after a per-tenant advisory lock. The hash is computed in the database. UPDATE, DELETE and TRUNCATE are refused even for the owner. Forged sequence, hash or time values are overwritten.
- **Roles in a tenant** (`memberships.role`): `admin`, `engineer`, `reviewer`, `viewer`, `auditor`. Actor types on audit entries: `user`, `api_key`, `worker`, `agent`, `system`.

## 7. API conventions

- **Base path** `/v1`. JSON in and out.
- **Errors** are RFC 9457 problem details with a stable `code` (`runs.scenario_not_approved`, `plans.contract_mismatch`, ...).
- **Lists** take `?limit=` (default 50, at most 200) and an opaque `?cursor=`; the answer has `items` and `next_cursor`.
- **Concurrency:** mutable resources return an `ETag`; updates need `If-Match` and answer `412` if it is stale.
- **Idempotency:** `POST /v1/assurance/runs` accepts `Idempotency-Key`; the same key and body return the same run.
- **Limits:** request bodies at most 1 MiB; login throttling; per-key rate limits.

## 8. Authentication and endpoints

| Area | Endpoints |
| --- | --- |
| Auth | `POST /v1/auth/login`, `/auth/refresh`, `/auth/logout`; `GET /v1/me`; `GET /v1/tenants` |
| Members | `GET, POST /v1/members`; `PATCH, DELETE /v1/members/{user_id}` |
| API keys | `GET, POST /v1/api-keys`; `GET, DELETE /v1/api-keys/{id}` |
| Projects | `GET, POST /v1/projects`; `GET, PATCH /v1/projects/{id}` |
| Assets and lineage | `GET, POST /v1/projects/{id}/assets`; `GET /v1/assets/{id}`; `GET, POST /v1/assets/{id}/inputs`; `DELETE /v1/assets/{id}/inputs/{input_id}` |
| Contracts | `GET, POST /v1/assets/{id}/contracts`; `GET /v1/assets/{id}/contracts/{version}` |
| Test cases and plans | `GET, POST /v1/projects/{id}/test-cases`; `GET, POST /v1/projects/{id}/plans` |
| Environments | `GET, POST /v1/environments`; `GET, PATCH /v1/environments/{id}`; `GET, POST /v1/environments/{id}/targets`; `DELETE .../targets/{target_id}` |
| Scenarios | `GET, POST /v1/projects/{id}/scenarios`; `GET /v1/scenarios/{id}`; `POST /v1/scenarios/{id}/approvals` |
| Runs | `POST, GET /v1/assurance/runs`; `GET /v1/assurance/runs/{id}`; `.../observations`; `.../lineage` |
| Recovery | `GET /v1/assurance/recovery` (measurements and attainment for a project and window) |
| Findings | `GET, POST /v1/projects/{id}/findings`; `GET, PATCH /v1/findings/{id}`; `POST /v1/findings/{id}/retest`; `POST /v1/findings/{id}/dismiss` |
| Waivers and gate | `POST, GET /v1/findings/{id}/waivers`; `GET /v1/waivers/{id}`; `POST /v1/waivers/{id}/decisions`; `GET /v1/projects/{id}/gate`; `GET /v1/projects/{id}/releases`; `GET /v1/projects/{id}/releases/{label}`; `POST /v1/projects/{id}/releases/{label}/decisions` |
| Evidence | `GET /v1/assurance/runs/{id}/evidence`: a run's bundle, built on demand from insert-only rows and signed when the server has a key (FR-13.1, FR-13.2); `GET /v1/evidence/public-key`; v0.1's `GET /v1/evidence/{id}/export` is not used, because nothing needs storing |

Sign-in uses Argon2id; the access token is a 15-minute JWT naming only the user; the tenant is chosen per request and checked against memberships. CI uses project API keys (`Authorization: Bearer rai_<prefix>_<secret>`, scopes `runs:start`, `runs:read`).

## 9. Contract format (YAML)

Excerpt of the reference contract, `contracts/sandboxbank/transactions_raw.yaml`:

```yaml
contract: transactions_raw
version: 1
owner: sandboxbank-payments
freshness: { column: created_at, max_lag: 1h }
primary_key: [txn_id]
unique_across_batches: [txn_id]
columns:
  txn_id:     { type: uuid, required: true }
  account_id: { type: string, required: true, pattern: "^ACC-[0-9]{7}$", references: accounts.account_id }
  channel:    { type: string, required: true, allowed: [card_present, ecommerce, ach, wire, p2p] }
  amount:
    type: decimal(14,2)
    required: true
    min: 0.01
    max: 25000
    max_lookup: { ref: accounts.txn_limit, key: account_id }
  is_fraud:   { type: boolean, required: true, severity: warn }
schema_change_policy:
  added_column: non_breaking
  removed_or_renamed_column: breaking
  type_change: breaking
severity_default: critical
```

- **Rules expand from the contract:** required, type, `min`/`max`, `allowed`, `pattern`, `references` (a reference table's column), `max_lookup` (a per-row bound from a reference table), freshness, the primary key within the batch, uniqueness across batches (against a per-tenant key history), and schema drift under `schema_change_policy`.
- **Severity and thresholds:** each rule has a severity (`info`, `warn`, `critical`; `severity_default` otherwise) and an optional `threshold`, a row count or a percentage such as `"0.1%"`. A critical failure beyond its threshold fails the batch (FR-03.4).
- **Strict parsing:** duplicate YAML keys are refused. The exact format is the JSON Schema committed in the repository. The reference contract expands to 36 rules and catches every Protocol section 4 defect class.
