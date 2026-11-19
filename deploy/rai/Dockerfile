# rai-api (B-36). Build from the repository root:
#   docker build -f deploy/rai/Dockerfile .
# Installs requirements.txt with every hash pinned (exported from uv.lock; kept in step by
# tests/unit/test_tooling.py), then the rai package itself without dependencies. The base
# image is pinned by digest and the build backend by hash (B-40: NFR-SEC-3).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY deploy/build-requirements.txt deploy/rai/requirements.txt ./
RUN pip install --require-hashes --only-binary=:all: -r build-requirements.txt -r requirements.txt
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation . && rm -rf /app/src

USER 10001
EXPOSE 8000
# Healthy = ready: the API answers and reaches its database (/readyz, NFR-OPS-1).
# Caddy starts only then. /readyz is not routed through Caddy; /healthz is.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)"]
# Plain HTTP on the private edge network only; Caddy terminates TLS in front of it.
CMD ["rai", "serve", "--host", "0.0.0.0", "--port", "8000"]
