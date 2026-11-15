# rai-chaos-agent (B-23). Standard library only: nothing to install but the package.
# TODO B-40: pin the base image by digest (NFR-SEC-3).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RAI_AGENT_STATE=/var/lib/rai-chaos-agent/state.json

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && mkdir -p /var/lib/rai-chaos-agent && chown 10001 /var/lib/rai-chaos-agent

USER 10001
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"]
CMD ["rai-chaos-agent", "serve"]
