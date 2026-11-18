"""End-to-end check of the TLS edge (B-36: NFR-SEC-2; threat I-06).

Run by ``make tls-e2e`` and the CI job "TLS edge (Caddy)" against the compose stack's
core profile (rai-db, rai-api, caddy). It checks, from the host:

1. HTTPS to Caddy works with Caddy's local CA, and the security headers are set;
2. the certificate is not trusted without that CA (it is real TLS, not a pass-through);
3. plain HTTP on the TLS port is not served;
4. rai-api has no host port: Caddy is the only way in;
5. the RAI CLI's client reaches the API through Caddy when told the CA (SSL_CERT_FILE).

Usage: python scripts/tls_e2e.py [--url https://localhost:8443]
"""

from __future__ import annotations

import argparse
import http.client
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

CA_PATH = "/data/caddy/pki/authorities/local/root.crt"


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments
        ["docker", "compose", "--profile", "core", *args],  # noqa: S607 - docker on PATH
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def fetch_ca(folder: Path) -> Path:
    target = folder / "caddy-root.crt"
    for _ in range(60):  # Caddy makes its CA when it first issues a certificate
        if compose("cp", f"caddy:{CA_PATH}", str(target)).returncode == 0:
            return target
        time.sleep(1)
    raise SystemExit("FAIL Caddy's local CA never appeared")


def get(url: str, context: ssl.SSLContext) -> http.client.HTTPResponse:
    response: http.client.HTTPResponse = urllib.request.urlopen(url, timeout=10, context=context)  # noqa: S310
    return response


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://localhost:8443")
    base = parser.parse_args().url.rstrip("/")
    results: list[bool] = []
    with tempfile.TemporaryDirectory() as tmp:
        ca = fetch_ca(Path(tmp))
        trusted = ssl.create_default_context(cafile=str(ca))
        response = None
        for _ in range(30):
            try:
                response = get(f"{base}/healthz", trusted)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(1)
        results.append(
            check("HTTPS through Caddy", response is not None and response.status == 200)
        )
        if response is not None:
            headers = response.headers
            results.append(
                check("HSTS", "max-age=31536000" in (headers["Strict-Transport-Security"] or ""))
            )
            results.append(check("nosniff", headers["X-Content-Type-Options"] == "nosniff"))
            results.append(
                check("no Server header", headers["Server"] is None, str(headers["Server"]))
            )

        try:
            get(f"{base}/healthz", ssl.create_default_context())
            results.append(check("untrusted without Caddy's CA", False, "the system trusted it"))
        except urllib.error.URLError as error:
            refused = isinstance(error.reason, ssl.SSLCertVerificationError)
            results.append(check("untrusted without Caddy's CA", refused))

        parts = urlsplit(base)
        plain = http.client.HTTPConnection(
            parts.hostname or "localhost", parts.port or 443, timeout=10
        )
        try:
            plain.request("GET", "/healthz")
            status = plain.getresponse().status
        except (OSError, http.client.HTTPException):
            status = 0
        results.append(check("no plain HTTP", status != 200, f"status {status}"))

        ports = compose("ps", "rai-api", "--format", "{{.Ports}}").stdout.strip()
        results.append(check("rai-api has no host port", bool(ports) and "->" not in ports,
                             ports))  # fmt: skip

        cli = subprocess.run(  # noqa: S603
            [sys.executable, "-c",
             "from rai.cli.run import UrllibHttp; import sys; "
             "r = UrllibHttp(sys.argv[1], 'none').request('GET', '/healthz'); "
             "sys.exit(0 if r.status == 200 else 1)", base],
            env=os.environ | {"SSL_CERT_FILE": str(ca)}, capture_output=True, text=True,
            check=False,
        )  # fmt: skip
        results.append(check("rai CLI client with SSL_CERT_FILE", cli.returncode == 0,
                             cli.stderr.strip()[-200:]))  # fmt: skip
    print("PASSED" if all(results) else "FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
