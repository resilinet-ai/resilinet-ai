# SandboxBank payments-api (B-03). Synthetic data only.
# TODO B-40: pin the base image by digest and install from the uv lockfile (NFR-SEC-3).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
CMD ["sandboxbank", "serve", "--host", "0.0.0.0", "--port", "8080"]
