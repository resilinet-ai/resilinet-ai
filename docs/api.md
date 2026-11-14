# RAI API

The REST API under `/v1` (Data Model sections 7 and 8). B-16 added sign-in, tenants and members; B-17 projects and test definitions; later items add runs and evidence. Synthetic data only.

## Run it locally

```bash
docker compose --profile core up -d          # rai-db on 127.0.0.1:55431
uv run rai db upgrade                        # as the owner (RAI_DATABASE_URL)
uv run rai db set-api-password               # password of the rai_api login role
uv run rai admin create-tenant --slug demo-cu --name "Demo CU" \
    --admin-email admin@example.test --admin-name "Demo Admin"
uv run rai serve                             # 127.0.0.1:8000
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

## Errors

Every error is RFC 9457 problem details (`application/problem+json`) with a stable `code`, for example `auth.invalid_credentials`, `tenant.not_found` or `members.last_admin`. Validation errors list each problem in `errors`. Request bodies over 1 MiB are refused with `413`.

## Audit

Every change to tenants and memberships is written to the tenant's hash-chained audit log by a database trigger, with the signed-in user as the actor. Check a chain with `rai audit verify --tenant <id>`.
