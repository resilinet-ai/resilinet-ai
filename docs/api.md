# RAI API

The REST API under `/v1` (Data Model sections 7 and 8). B-16 adds sign-in, tenants and members; later items add projects, contracts, runs and evidence. Synthetic data only.

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

## Errors

Every error is RFC 9457 problem details (`application/problem+json`) with a stable `code`, for example `auth.invalid_credentials`, `tenant.not_found` or `members.last_admin`. Validation errors list each problem in `errors`. Request bodies over 1 MiB are refused with `413`.

## Audit

Every change to tenants and memberships is written to the tenant's hash-chained audit log by a database trigger, with the signed-in user as the actor. Check a chain with `rai audit verify --tenant <id>`.
