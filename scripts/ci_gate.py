"""Seed a live RAI for the CI gate job (B-20: FR-06.1). Synthetic data only.

    python scripts/ci_gate.py data OUT   # SandboxBank slice + worker connector config
    python scripts/ci_gate.py seed       # through the API: project, plans, API key

``seed`` signs in as the tenant admin (RAI_GATE_EMAIL, RAI_NEW_PASSWORD) and
invites an engineer, whose first password is set as the operator would
(``rai admin set-password``, owner connection RAI_DATABASE_URL). The engineer
publishes the definitions and creates the key, which is masked in the job log and
written to ``GITHUB_OUTPUT`` as ``api-key``. Two plans:

* ``ci-clean``: the first batch, which passes;
* ``ci-resent``: the first batch, then one that re-sends rows from it, which fails
  ``unique_across_batches:txn_id``: the gate must block.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from rai.cli.run import UrllibHttp
from rai.core.accounts import set_password
from rai.core.db import database_url
from rai.core.passwords import hash_password
from sandboxbank.fixtures import build_slice

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml"
ENGINEER = "gate-engineer@example.test"
REFERENCES = {"accounts": "accounts.csv", "merchants": "merchants.csv"}
PLANS = {"ci-clean": ["batch_0"], "ci-resent": ["batch_0", "batch_1"]}


def data(out: Path) -> None:
    build_slice(
        out / "slice", data_seed=27, injector_seed=28, accounts=200, merchants=40,
        rows_per_batch=2_000,
    )  # fmt: skip
    connectors = out / "connectors"
    connectors.mkdir(parents=True, exist_ok=True)
    root = (out / "slice" / "feed").as_posix()
    config = f"name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: {root}\n"
    (connectors / "feed.yaml").write_text(config, encoding="utf-8")


def _call(http: UrllibHttp, method: str, path: str, body: Any = None) -> Any:
    reply = http.request(method, path, body=body)
    if reply.status not in (200, 201, 202):
        raise SystemExit(f"{method} {path}: HTTP {reply.status} {reply.body}")
    return reply.body


def _sign_in(api_url: str, email: str, password: str) -> UrllibHttp:
    tokens = _call(UrllibHttp(api_url, ""), "POST", "/v1/auth/login",
                   {"email": email, "password": password})  # fmt: skip
    return UrllibHttp(api_url, tokens["access_token"])


def seed(api_url: str) -> str:
    password = os.environ["RAI_NEW_PASSWORD"]
    admin = _sign_in(api_url, os.environ["RAI_GATE_EMAIL"], password)
    # Tests run only in a registered environment (B-22, deny by default).
    _call(admin, "POST", "/v1/environments", {"name": "SANDBOX-01", "type": "SANDBOX"})
    invite = {"email": ENGINEER, "display_name": "Gate Engineer", "role": "engineer"}
    _call(admin, "POST", "/v1/members", invite)
    with create_engine(database_url()).begin() as conn:
        set_password(conn, email=ENGINEER, password_hash=hash_password(password))
    engineer = _sign_in(api_url, ENGINEER, password)

    project = _call(engineer, "POST", "/v1/projects", {"name": "CI gate"})["project_id"]
    tests = {}
    for name, batch in (("batch_0", "B000000"), ("batch_1", "B000001")):
        locator = f"transactions_raw/{batch}.csv"
        asset_body = {"kind": "table", "name": name, "locator": locator}
        asset = _call(engineer, "POST", f"/v1/projects/{project}/assets", asset_body)["asset_id"]
        source = {"source": CONTRACT.read_text(encoding="utf-8")}
        _call(engineer, "POST", f"/v1/assets/{asset}/contracts", source)
        test = {
            "name": f"contract.{name}", "type": "contract_rule", "target_asset_id": asset,
            "definition": {"connector": "feed", "environment": "SANDBOX-01",
                           "references": REFERENCES},
        }  # fmt: skip
        tests[name] = _call(engineer, "POST", f"/v1/projects/{project}/test-cases", test)["test_id"]
    for plan, names in PLANS.items():
        items = [{"test_id": tests[n]} for n in names]
        _call(engineer, "POST", f"/v1/projects/{project}/plans", {"name": plan, "items": items})
    key_body = {"project_id": project, "name": "ci-gate", "expires_in_days": 1}
    token: str = _call(engineer, "POST", "/v1/api-keys", key_body)["key"]
    return token


def main(argv: list[str]) -> None:
    command, *rest = argv
    if command == "data":
        data(Path(rest[0]))
    elif command == "seed":
        token = seed(os.environ.get("RAI_API_URL", "http://127.0.0.1:8000"))
        sys.stdout.write(f"::add-mask::{token}\n")  # GitHub Actions: hide it in the log
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write(f"api-key={token}\n")
    else:
        raise SystemExit(f"unknown command {command!r}; use data or seed")


if __name__ == "__main__":
    main(sys.argv[1:])
