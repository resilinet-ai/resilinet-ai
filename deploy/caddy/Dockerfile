# Caddy with the web UI built in (B-36, FR-11.2). Build from the repository root:
#   docker build -f deploy/caddy/Dockerfile .
# Stage 1 builds web/ from its lockfile (npm ci, no install scripts) and runs its checks;
# stage 2 is Caddy serving the static files and proxying /v1 to rai-api.
# TODO B-40: pin both base images by digest (NFR-SEC-3).
FROM node:24-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit
COPY web/ ./
RUN npm run check

FROM caddy:2.10.2
COPY deploy/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv
