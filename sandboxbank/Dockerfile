# SandboxBank payments-api and etl-job (B-03). Synthetic data only. Build from the
# repository root:
#   docker build -f sandboxbank/Dockerfile .
# The base image is pinned by digest, and every package by hash (B-40: NFR-SEC-3):
# requirements.txt is exported from uv.lock (tests/unit/test_tooling.py keeps it in step),
# and the build backend comes from deploy/build-requirements.txt.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1     PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY deploy/build-requirements.txt sandboxbank/requirements.txt ./
RUN pip install --require-hashes --only-binary=:all: -r build-requirements.txt -r requirements.txt
COPY sandboxbank/pyproject.toml ./
COPY sandboxbank/src ./src
RUN pip install --no-deps --no-build-isolation . && rm -rf /app/src

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5     CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
CMD ["sandboxbank", "serve", "--host", "0.0.0.0", "--port", "8080"]
