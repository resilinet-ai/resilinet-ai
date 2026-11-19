# Caddy with the web UI built in (B-36, FR-11.2). Build from the repository root:
#   docker build -f deploy/caddy/Dockerfile .
# Stage 1 builds web/ from its lockfile (npm ci, no install scripts) and runs its checks;
# stage 2 is Caddy serving the static files and proxying /v1 to rai-api.
# Both base images are pinned by digest (B-40: NFR-SEC-3).
FROM node:25-bookworm-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-fund --no-audit
COPY web/ ./
RUN npm run check

FROM caddy:2.11.8@sha256:4fcafa98afd1c2e642e353e3c5cf9ddbe892fcee14bc990e7ba2c5b78e51f9cf
# CVE-2026-4738 (critical, fixed in zlib 1.4-r0) is not in the pinned base yet. apk checks
# Alpine's package signatures; drop this once Dependabot brings a base that has the fix.
RUN apk upgrade --no-cache zlib
COPY deploy/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv
