"""Seed a live RAI for the CI gate job (B-20: FR-06.1). Synthetic data only.

    python scripts/ci_gate.py data OUT   # SandboxBank slice + worker connector config
    python scripts/ci_gate.py seed       # through the API: project, plans, API key
    python scripts/ci_gate.py bundles OUT RUN_ID...   # export the runs' evidence bundles

``seed`` signs in as the tenant admin (RAI_GATE_EMAIL, RAI_NEW_PASSWORD) and
invites an engineer, whose first password is set as the operator would
(``rai admin set-password``, owner connection RAI_DATABASE_URL). The engineer
publishes the definitions and creates the key, which is masked in the job log and
written to ``GITHUB_OUTPUT`` as ``api-key``. Three plans:

* ``ci-clean``: the first batch, which passes its contract and has no canary value
  in it (FR-05.1);
* ``ci-resent``: the first batch, then one that re-sends rows from it, which fails
  ``unique_across_batches:txn_id``: the gate must block;
* ``ci-leak``: a CRM export with canary values planted in its notes, which the
  canary check must fail, masking every value.

``data`` also writes SandboxBank's canaries; their key, ``needles.csv``, is the key the
job's canary sweep of logs and evidence bundles uses (``rai canary scan``).
``bundles`` exports the evidence bundles of the runs it is given, with the API key
in RAI_API_KEY.
"""

from __future__ import annotations

import csv
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from rai.cli.run import UrllibHttp
from rai.core.accounts import set_password
from rai.core.db import database_url
from rai.core.passwords import hash_password
from sandboxbank.canaries import NEEDLES_FILE, generate_canaries, write_canaries
from sandboxbank.fixtures import build_slice

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml"
ENGINEER = "gate-engineer@example.test"
REFERENCES = {"accounts": "accounts.csv", "merchants": "merchants.csv"}
PLANS = {
    "ci-clean": ["batch_0", "privacy.batch_0"],
    "ci-resent": ["batch_0", "batch_1"],
    "ci-leak": ["privacy.crm_export"],
}
CANARIES = "canaries"  # under the feed root: customers.csv, canaries.json, needles.csv


def data(out: Path) -> None:
    build_slice(
        out / "slice", data_seed=27, injector_seed=28, accounts=200, merchants=40,
        rows_per_batch=2_000,
    )  # fmt: skip
    feed = out / "slice" / "feed"
    with (feed / "accounts.csv").open(newline="", encoding="utf-8") as handle:
        customers = [row["customer_id"] for row in csv.DictReader(handle)]
    made = generate_canaries(29, customers, 5)
    write_canaries(made, 29, feed / CANARIES)
    leak = made[0]
    with (feed / "crm_export.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("customer_id", "notes"))
        writer.writerow(("CUS-0000001", "called about a card"))
        writer.writerow((leak.customer_id, f"prefers email: {leak.email.upper()}"))
        writer.writerow((made[1].customer_id, f"card on file {made[1].card_number}"))
    connectors = out / "connectors"
    connectors.mkdir(parents=True, exist_ok=True)
    root = feed.as_posix()
    config = f"name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: {root}\n"
    (connectors / "feed.yaml").write_text(config, encoding="utf-8")


def call(http: UrllibHttp, method: str, path: str, body: Any = None) -> Any:
    reply = http.request(method, path, body=body)
    if reply.status not in (200, 201, 202):
        raise SystemExit(f"{method} {path}: HTTP {reply.status} {reply.body}")
    return reply.body


def sign_in(api_url: str, email: str, password: str) -> UrllibHttp:
    tokens = call(UrllibHttp(api_url, ""), "POST", "/v1/auth/login",
                   {"email": email, "password": password})  # fmt: skip
    return UrllibHttp(api_url, tokens["access_token"])


def seed(api_url: str) -> str:
    password = os.environ["RAI_NEW_PASSWORD"]
    admin = sign_in(api_url, os.environ["RAI_GATE_EMAIL"], password)
    # Tests run only in a registered environment (B-22, deny by default).
    call(admin, "POST", "/v1/environments", {"name": "SANDBOX-01", "type": "SANDBOX"})
    invite = {"email": ENGINEER, "display_name": "Gate Engineer", "role": "engineer"}
    call(admin, "POST", "/v1/members", invite)
    with create_engine(database_url()).begin() as conn:
        set_password(conn, email=ENGINEER, password_hash=hash_password(password))
    engineer = sign_in(api_url, ENGINEER, password)

    project = call(engineer, "POST", "/v1/projects", {"name": "CI gate"})["project_id"]
    tests = {}
    assets = {}
    for name, batch in (("batch_0", "B000000"), ("batch_1", "B000001")):
        locator = f"transactions_raw/{batch}.csv"
        asset_body = {"kind": "table", "name": name, "locator": locator}
        asset = call(engineer, "POST", f"/v1/projects/{project}/assets", asset_body)["asset_id"]
        assets[name] = asset
        source = {"source": CONTRACT.read_text(encoding="utf-8")}
        call(engineer, "POST", f"/v1/assets/{asset}/contracts", source)
        test = {
            "name": f"contract.{name}", "type": "contract_rule", "target_asset_id": asset,
            "definition": {"connector": "feed", "environment": "SANDBOX-01",
                           "references": REFERENCES},
        }  # fmt: skip
        tests[name] = call(engineer, "POST", f"/v1/projects/{project}/test-cases", test)["test_id"]
    for name, locator in (
        ("canary_key", f"{CANARIES}/{NEEDLES_FILE}"),
        ("crm_export", "crm_export.csv"),
    ):
        body = {"kind": "table", "name": name, "locator": locator}
        assets[name] = call(engineer, "POST", f"/v1/projects/{project}/assets", body)["asset_id"]
    for target in ("batch_0", "crm_export"):
        test = {
            "name": f"privacy.{target}", "type": "privacy_canary",
            "target_asset_id": assets[target],
            "definition": {"connector": "feed", "environment": "SANDBOX-01",
                           "key": {"asset": "canary_key", "connector": "feed"}},
        }  # fmt: skip
        tests[f"privacy.{target}"] = call(
            engineer, "POST", f"/v1/projects/{project}/test-cases", test
        )["test_id"]
    for plan, names in PLANS.items():
        items = [{"test_id": tests[n]} for n in names]
        call(engineer, "POST", f"/v1/projects/{project}/plans", {"name": plan, "items": items})
    key_body = {"project_id": project, "name": "ci-gate", "expires_in_days": 1}
    token: str = call(engineer, "POST", "/v1/api-keys", key_body)["key"]
    return token


def bundles(api_url: str, out: Path, run_ids: list[str]) -> None:
    """Each run's evidence bundle, as ``rai-evidence-<run_id>.zip`` in OUT."""
    out.mkdir(parents=True, exist_ok=True)
    key = os.environ["RAI_API_KEY"]
    for run_id in run_ids:
        request = urllib.request.Request(  # noqa: S310 - a localhost URL in CI
            f"{api_url}/v1/assurance/runs/{run_id}/evidence",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            (out / f"rai-evidence-{run_id}.zip").write_bytes(response.read())
        print(f"exported the evidence bundle of run {run_id}")


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
    elif command == "bundles":
        api_url = os.environ.get("RAI_API_URL", "http://127.0.0.1:8000")
        bundles(api_url, Path(rest[0]), [r for r in rest[1:] if r])
    else:
        raise SystemExit(f"unknown command {command!r}; use data, seed or bundles")


if __name__ == "__main__":
    main(sys.argv[1:])
