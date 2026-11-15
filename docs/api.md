# RAI API

The REST API under `/v1` (Data Model sections 7 and 8). B-16 added sign-in, tenants and members; B-17 projects and test definitions; B-18 runs and the worker; B-19 lineage; later items add evidence. Synthetic data only.

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
| `GET, POST /v1/projects/{id}/plans` | engineer | Ordered items: test cases, optionally pinned to a contract version |
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

## Runs

A run executes one plan version. It **pins every input** when it is queued: the plan, each test-case version with its definition, the target asset, the contract version with its SHA-256, and the target's declared upstream graph. The worker executes from the pins alone.

| Endpoint | Write roles | Notes |
| --- | --- | --- |
| `POST /v1/assurance/runs` | engineer | `{"plan_id": ..., "trigger_ref": "<commit SHA>"}`; answers `202` with the run |
| `GET /v1/assurance/runs` | | Newest first; `?project_id=`, `?state=`, `?since=` |
| `GET /v1/assurance/runs/{id}` | | State, verdict, reason, pins, steps, observation counts by outcome |
| `GET /v1/assurance/runs/{id}/observations` | | One per rule; `?outcome=fail` |
| `GET /v1/assurance/runs/{id}/lineage` | | Every hop with the SHA-256 of the data read (see [Lineage](#lineage)) |

**States:** `queued → preflight → running → evaluating → passed | failed`.
- **`refused`:** preflight found something this worker cannot run. The reason lists every problem.
- **`error`:** the platform failed three times.
- **`aborted`:** reserved for the kill-switch.

Finished runs never change; to try again, start a new run.

**`Idempotency-Key`:** sending the same request with the same key returns the same run, which is safe for CI retries. Reusing a key for a different request is `422`.

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
- **Not yet supported:** recovery assertions, consistency checks, TEVV metrics and drift checks. A plan containing them is refused with a reason, never passed silently.

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

## Errors

Every error is RFC 9457 problem details (`application/problem+json`) with a stable `code`, for example `auth.invalid_credentials`, `tenant.not_found` or `members.last_admin`. Validation errors list each problem in `errors`. Request bodies over 1 MiB are refused with `413`.

## Audit

Every change to tenants and memberships is written to the tenant's hash-chained audit log by a database trigger, with the signed-in user as the actor. Check a chain with `rai audit verify --tenant <id>`.
