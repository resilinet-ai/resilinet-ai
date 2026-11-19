# Caddy with the web UI built in (B-36, FR-11.2). Build from the repository root:
#   docker build -f deploy/caddy/Dockerfile .
# Stage 1 builds web/ from its lockfile (npm ci, no install scripts) and runs its checks;
# stage 2 is Caddy serving the static files and proxying /v1 to rai-api.
# Both base images are pinned by digest (B-40: NFR-SEC-3).
FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit
COPY web/ ./
RUN npm run check

FROM caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb
COPY deploy/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv
