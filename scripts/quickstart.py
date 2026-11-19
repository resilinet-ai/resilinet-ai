"""Seed a local RAI for the quick-start (FR-14.1, docs/quickstart.md). Synthetic data only.

    python -m scripts.quickstart data .rai/quickstart   # SandboxBank batches + worker connector
    python -m scripts.quickstart seed                   # through the API: a project and two runs

``seed`` signs in as the tenant's admin (``RAI_QUICKSTART_EMAIL``, default
``admin@example.test``; password ``RAI_NEW_PASSWORD``) and, through the API:

* registers the environment ``SANDBOX-01`` (tests run only in a registered one);
* invites an engineer and a reviewer, whose first passwords are set as an operator
  would (``rai admin set-password``; owner connection ``RAI_DATABASE_URL``), the
  same as the admin's, so you can sign in as each of them;
* creates the project "Quick start": two batch assets with the SandboxBank contract,
  a test case for each, and two plans, ``clean`` (the first batch, which passes) and
  ``resent`` (the first batch, then one that re-sends 2 of its rows, which fails);
* starts one run of each plan. A worker (``rai worker``) executes them.

It never deletes anything; run it once per tenant.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine

from rai.cli.run import UrllibHttp
from rai.core.accounts import set_password
from rai.core.db import database_url
from rai.core.passwords import hash_password
from scripts.ci_gate import CONTRACT, REFERENCES, call, data, sign_in

PROJECT = "Quick start"
ENVIRONMENT = "SANDBOX-01"
PEOPLE = {
    "engineer": ("quickstart-engineer@example.test", "Quick-start Engineer"),
    "reviewer": ("quickstart-reviewer@example.test", "Quick-start Reviewer"),
}
PLANS = {"clean": ["batch_0"], "resent": ["batch_0", "batch_1"]}


def _environment(admin: UrllibHttp) -> None:
    existing = call(admin, "GET", "/v1/environments?limit=200")["items"]
    if not any(e["name"] == ENVIRONMENT for e in existing):
        call(admin, "POST", "/v1/environments", {"name": ENVIRONMENT, "type": "SANDBOX"})


def _people(admin: UrllibHttp, password: str) -> None:
    known = {m["email"] for m in call(admin, "GET", "/v1/members")}  # a plain list
    with create_engine(database_url()).begin() as conn:
        for role, (email, name) in PEOPLE.items():
            if email not in known:
                call(admin, "POST", "/v1/members",
                     {"email": email, "display_name": name, "role": role})  # fmt: skip
                set_password(conn, email=email, password_hash=hash_password(password))


def seed(api_url: str, admin_email: str, password: str) -> dict[str, str]:
    """Create the quick-start project and start its two runs; returns plan -> run id."""
    admin = sign_in(api_url, admin_email, password)
    projects = call(admin, "GET", "/v1/projects?limit=200")["items"]
    if any(p["name"] == PROJECT for p in projects):
        raise SystemExit(f"this tenant already has a project called {PROJECT!r}; nothing done")
    _environment(admin)
    _people(admin, password)
    engineer = sign_in(api_url, PEOPLE["engineer"][0], password)

    body = {"name": PROJECT, "description": "SandboxBank batches from docs/quickstart.md"}
    project = call(engineer, "POST", "/v1/projects", body)["project_id"]
    tests = {}
    for name, batch in (("batch_0", "B000000"), ("batch_1", "B000001")):
        asset_body = {"kind": "table", "name": name, "locator": f"transactions_raw/{batch}.csv"}
        asset = call(engineer, "POST", f"/v1/projects/{project}/assets", asset_body)["asset_id"]
        call(engineer, "POST", f"/v1/assets/{asset}/contracts",
             {"source": CONTRACT.read_text(encoding="utf-8")})  # fmt: skip
        test = {
            "name": f"contract.{name}", "type": "contract_rule", "target_asset_id": asset,
            "definition": {"connector": "feed", "environment": ENVIRONMENT,
                           "references": REFERENCES},
        }  # fmt: skip
        tests[name] = call(engineer, "POST", f"/v1/projects/{project}/test-cases", test)["test_id"]
    runs = {}
    for plan, names in PLANS.items():
        items = [{"test_id": tests[n]} for n in names]
        plan_id = call(engineer, "POST", f"/v1/projects/{project}/plans",
                       {"name": plan, "items": items})["plan_id"]  # fmt: skip
        runs[plan] = call(engineer, "POST", "/v1/assurance/runs",
                          {"plan_id": plan_id, "trigger_ref": "quickstart"})["run_id"]  # fmt: skip
    return runs


def main(argv: list[str]) -> int:
    if argv[:1] == ["data"] and len(argv) == 2:
        out = Path(argv[1])
        data(out)
        print(f"SandboxBank batches in {out / 'slice'}; worker connector in {out / 'connectors'}")
        print(f"Start the worker with RAI_WORKER_CONNECTORS={(out / 'connectors').as_posix()}")
        return 0
    if argv == ["seed"]:
        password = os.environ.get("RAI_NEW_PASSWORD")
        if not password:
            print("set RAI_NEW_PASSWORD to the tenant admin's password", file=sys.stderr)
            return 2
        api = os.environ.get("RAI_API_URL", "http://127.0.0.1:8000")
        runs = seed(api, os.environ.get("RAI_QUICKSTART_EMAIL", "admin@example.test"), password)
        print(f"Project {PROJECT!r} created; runs queued:")
        for plan, run_id in runs.items():
            print(f"  {plan:7} {run_id}")
        print("Sign in as the admin, quickstart-engineer@example.test or "
              "quickstart-reviewer@example.test, with the same password.")  # fmt: skip
        return 0
    print("usage: python -m scripts.quickstart data DIR | seed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
