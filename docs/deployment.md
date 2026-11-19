# Running RAI behind TLS

The `core` compose profile runs the RAI database, the API and Caddy (B-36: NFR-SEC-2). Caddy is the only way in: it terminates HTTPS and forwards to the API over a private network. The worker still runs on the host (`rai worker`) for now.

```
client --HTTPS--> caddy (:8443 on the host, :443 inside) --edge_net--> rai-api:8000 --control_net--> rai-db
                   └─ /: the web UI, built into the Caddy image
```

## First start

```bash
cp .env.example .env              # set RAI_DB_PASSWORD, RAI_API_DB_PASSWORD and RAI_JWT_SECRET
docker compose --profile core up -d --wait rai-db
uv run rai db upgrade             # RAI_DATABASE_URL: the rai owner role, through 127.0.0.1:55431
uv run rai db set-api-password    # reads RAI_API_DB_PASSWORD
docker compose --profile core up -d --build --wait
curl --cacert <(docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt -) \
     https://localhost:8443/healthz
```

`make tls-e2e` does all of this with throwaway credentials, checks the result, and removes the stack again. The CI job "TLS edge (Caddy)" runs the same check on every pull request.

## What the edge guarantees

- **HTTPS only.** Caddy listens on 443 and nothing else. There is no plain-HTTP listener and so no redirect: a plain HTTP request to the TLS port is refused.
- **The API has no host port.** `rai-api` is on `edge_net`, an internal network shared only with Caddy, and on `control_net` for the database. It never joins `sandbox_net`.
- **One origin for the UI and the API.** `/v1` and `/healthz` go to the API; everything else is the web UI (`web/`), built into the Caddy image after its checks pass. The UI gets a Content-Security-Policy that allows only this origin (`default-src 'self'`, `script-src 'self'`, `frame-ancestors 'none'`).
- **Headers.** HSTS (one year), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`; no `Server` header. Request bodies are limited to 2 MB at the edge (the API's own limit is 1 MiB).
- **Real client addresses.** Caddy replaces any `X-Forwarded-For` a client sends with the address it sees. `rai serve --forwarded-allow-ips` trusts that header only from Caddy's fixed address on `edge_net` (`172.31.236.2`), so the per-address login limit counts clients, not the proxy. `rai serve` refuses `*`.
- **Locked-down containers.** Both are read-only, drop every capability (Caddy keeps only `NET_BIND_SERVICE`), and cannot gain privileges. The API image installs exactly the versions in `uv.lock`, with hashes, and runs as a non-root user.
- **Pinned, scanned and signed images.** Every base image is pinned by digest; CI records an SBOM for each image, fails on a fixable critical vulnerability, and signs the images it publishes from `main` ([`supply-chain.md`](supply-chain.md)).
- **Host-only by default.** Caddy publishes `127.0.0.1:8443`. To serve a network, set `RAI_HTTPS_BIND=0.0.0.0` and `RAI_HTTPS_PORT=443`.

## Certificates

| `RAI_TLS` | Certificate | Use |
| --- | --- | --- |
| `internal` (default) | From Caddy's own local CA, kept in the `caddy-data` volume | Development, and installs without a public name |
| an email address | From Let's Encrypt (ACME), through the TLS-ALPN challenge on port 443 | A public name in `RAI_SITE_ADDRESS`, reachable on 443 |

With `internal`, clients must trust Caddy's root certificate, `/data/caddy/pki/authorities/local/root.crt` in the `caddy` container. The RAI CLI (`rai run`, the GitHub Action) uses the system's trust store, and Python honours `SSL_CERT_FILE`:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./rai-root.crt
SSL_CERT_FILE=./rai-root.crt RAI_API_URL=https://localhost:8443 uv run rai run --plan ci-contracts
```

## Logs and health

Every container logs JSON lines (`docker compose logs rai-api`), and each API request and worker run is a span with a W3C trace id; the API's Docker healthcheck is its database-aware `/readyz`. See [observability.md](observability.md).

## Not covered yet

- Traffic inside the host (Caddy to the API, the API to the database) is not encrypted: one host, private Docker networks (threat model, assumptions). Mutual TLS is the upgrade path.
- The worker is not a compose service yet (B-18).
- Compose builds the images locally rather than pulling the signed ones from GHCR, so it does not verify signatures ([`supply-chain.md`](supply-chain.md)).
