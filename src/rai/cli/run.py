"""``rai run``: start a test plan through the API and gate on its verdict (B-20: FR-06.1).

Exit codes match ``rai validate``:

* 0: the run passed and the release gate passed (or, with ``--no-wait``, it was queued);
* 1: the run failed (a blocking rule failed), or the project's release gate is
  blocked by an open critical finding without a waiver in force (B-33: FR-12.2);
* 2: the run could not give a verdict (refused, error, aborted, timed out) or
  the API could not be used (unreachable, bad key, unknown plan).

The API key is read from ``RAI_API_KEY``, never from the command line. Plain
``http://`` is refused except to this machine, so a key is never sent in clear
over a network. In GitHub Actions the outcome is also written to the job summary
(``GITHUB_STEP_SUMMARY``) and to step outputs (``GITHUB_OUTPUT``).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Protocol
from uuid import UUID

import typer

from rai import __version__

API_KEY_ENV = "RAI_API_KEY"
API_URL_ENV = "RAI_API_URL"
TERMINAL = {"passed", "failed", "refused", "aborted", "error"}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
MAX_POLL_ERRORS = 5


class RunProblem(Exception):
    """The run could not be started or followed (exit 2)."""


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any


class Http(Protocol):
    def request(self, method: str, path: str, *, body: Any = None,
                headers: dict[str, str] | None = None) -> Reply: ...  # fmt: skip


@dataclass
class UrllibHttp:
    """The standard library's HTTP client: no extra dependency in CI."""

    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        url = urllib.parse.urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise RunProblem(f"--api-url must be an http(s) URL, not {self.base_url!r}")
        if url.scheme == "http" and url.hostname not in LOOPBACK:
            raise RunProblem("--api-url must use https (http is allowed only to localhost)")
        self.base_url = self.base_url.rstrip("/")

    def request(
        self, method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None
    ) -> Reply:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __post_init__
            self.base_url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": f"rai/{__version__}"}
            | (headers or {}),
        )  # fmt: skip
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return Reply(response.status, _json(response.read()))
        except urllib.error.HTTPError as error:
            return Reply(error.code, _json(error.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise RunProblem(f"could not reach the RAI API at {self.base_url}: {reason}") from None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _explain(reply: Reply, doing: str) -> RunProblem:
    body = reply.body if isinstance(reply.body, dict) else {}
    title = body.get("title") or f"HTTP {reply.status}"
    detail = body.get("detail") or "; ".join(
        str(e.get("message") or e) for e in body.get("errors", []) if isinstance(e, dict)
    )
    return RunProblem(f"{doing}: {title}" + (f" ({detail})" if detail else ""))


@dataclass
class Outcome:
    code: int
    run: dict[str, Any] | None = None
    failing: list[dict[str, Any]] = field(default_factory=list)
    release: dict[str, Any] | None = None  # the project's release gate, once there is a verdict


@dataclass
class Gate:
    http: Http
    say: Callable[[str], None]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def start(self, plan: str, project: str | None, trigger_ref: str | None,
              idempotency_key: str | None) -> dict[str, Any]:  # fmt: skip
        body: dict[str, Any] = {"trigger_ref": trigger_ref}
        try:
            body["plan_id"] = str(UUID(plan))
        except ValueError:
            body["plan"] = plan
            if project:
                body["project_id"] = project
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        reply = self.http.request("POST", "/v1/assurance/runs", body=body, headers=headers)
        if reply.status not in (200, 202) or not isinstance(reply.body, dict):
            raise _explain(reply, f"could not start plan {plan!r}")
        run: dict[str, Any] = reply.body
        return run

    def wait(self, run_id: str, timeout: float, interval: float) -> dict[str, Any]:
        deadline = self.clock() + timeout
        errors = 0
        while True:
            reply = self.http.request("GET", f"/v1/assurance/runs/{run_id}")
            if reply.status == 200 and isinstance(reply.body, dict):
                errors = 0
                run: dict[str, Any] = reply.body
                if run["state"] in TERMINAL:
                    return run
            elif reply.status >= 500 or reply.status == 429:
                errors += 1
                if errors > MAX_POLL_ERRORS:
                    raise _explain(reply, f"could not follow run {run_id}")
            else:
                raise _explain(reply, f"could not follow run {run_id}")
            if self.clock() >= deadline:
                raise RunProblem(
                    f"run {run_id} has no verdict after {timeout:.0f} s; it keeps running"
                )
            self.sleep(interval)

    def failing(self, run_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for outcome in ("fail", "error"):
            reply = self.http.request(
                "GET", f"/v1/assurance/runs/{run_id}/observations?outcome={outcome}&limit=50"
            )
            if reply.status == 200 and isinstance(reply.body, dict):
                found.extend(reply.body.get("items", []))
        return found

    def release_gate(self, project_id: str) -> dict[str, Any]:
        reply = self.http.request("GET", f"/v1/projects/{project_id}/gate")
        if reply.status != 200 or not isinstance(reply.body, dict):
            raise _explain(reply, "could not read the release gate")
        verdict: dict[str, Any] = reply.body
        return verdict

    def run(self, plan: str, *, project: str | None = None, trigger_ref: str | None = None,
            idempotency_key: str | None = None, wait: bool = True, timeout: float = 1800,
            interval: float = 3, release_gate: bool = True) -> Outcome:  # fmt: skip
        try:
            run = self.start(plan, project, trigger_ref, idempotency_key)
            plan_text = f"plan {plan}" + (f" @ {trigger_ref[:12]}" if trigger_ref else "")
            self.say(f"run {run['run_id']} {run['state']} ({plan_text})")
            if not wait:
                return Outcome(0, run)
            run = self.wait(run["run_id"], timeout, interval)
        except RunProblem as problem:
            self.say(f"ERROR {problem}")
            return Outcome(2)
        failing = self.failing(run["run_id"]) if run["state"] in ("failed", "error") else []
        self._report(run, failing)
        code = {"passed": 0, "failed": 1}.get(run["state"], 2)
        if not release_gate or code == 2:
            return Outcome(code, run, failing)
        try:
            release = self.release_gate(run["project_id"])
        except RunProblem as problem:
            self.say(f"ERROR {problem}")
            return Outcome(2, run, failing)
        self._report_gate(release)
        if release["verdict"] != "pass":
            code = 1
        return Outcome(code, run, failing, release)

    def _report_gate(self, release: dict[str, Any]) -> None:
        if release["verdict"] == "pass":
            waived = release["waived_count"]
            self.say("GATE pass" + (f" ({waived} critical finding(s) waived)" if waived else ""))
            for f in release["waived"]:
                self.say(f"  WAIVED {f['finding_id']} {f['title']} until {f['waiver_expires_at']}")
            return
        count = release["blocking_count"]
        self.say(f"GATE blocked: {count} open critical finding(s) without a waiver in force")
        for f in release["blocking"]:
            self.say(f"  BLOCKS {f['finding_id']} {f['title']}")

    def _report(self, run: dict[str, Any], failing: list[dict[str, Any]]) -> None:
        steps = ", ".join(f"{s['step_key']} {s['verdict'] or s['state']}" for s in run["steps"])
        verdict = run["state"].upper()
        self.say(f"{verdict} run {run['run_id']}" + (f": {steps}" if steps else ""))
        if run.get("reason"):
            self.say(f"  reason: {run['reason']}")
        for o in failing:
            counts = f"{o['failing_count']:,}/{o['row_count']:,} rows" if o["row_count"] else ""
            severity = (o.get("expected") or {}).get("severity", "")
            message = (o.get("observed_value") or {}).get("message", "")
            label = f"{o['rule']} ({severity})" if severity else o["rule"]
            line = f"  {o['outcome'].upper():5} {o['step_key']} {label} {counts} {message}"
            self.say(line.rstrip())


def _github_outputs(outcome: Outcome, api_url: str) -> None:
    """Step outputs and a job summary when running in GitHub Actions."""
    run = outcome.run or {}
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"run-id={run.get('run_id', '')}\n")
            handle.write(f"state={run.get('state', '')}\n")
            handle.write(f"verdict={run.get('verdict') or ''}\n")
            handle.write(f"gate={(outcome.release or {}).get('verdict', '')}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and run:
        icon = {0: "PASSED", 1: "FAILED"}.get(outcome.code, "NO VERDICT")
        lines = [
            f"### RAI gate: {icon}", "",
            f"Run `{run['run_id']}` on {api_url}: state **{run['state']}**.", "",
        ]  # fmt: skip
        if run.get("reason"):
            lines += [f"Reason: {run['reason']}", ""]
        if outcome.failing:
            lines += ["| Step | Rule | Outcome | Failing rows |", "| --- | --- | --- | --- |"]
            lines += [
                f"| {o['step_key']} | `{o['rule']}` | {o['outcome']} | {o['failing_count'] or ''} |"
                for o in outcome.failing
            ]
        if outcome.release:
            release = outcome.release
            counts = f"{release['blocking_count']} blocking, {release['waived_count']} waived"
            lines += ["", f"Release gate: **{release['verdict']}** ({counts})."]
            if release["blocking"]:
                lines += ["", "| Blocking finding | Title |", "| --- | --- |"]
                lines += [f"| `{f['finding_id']}` | {f['title']} |" for f in release["blocking"]]
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def run_command(
    plan: Annotated[str, typer.Option("--plan", help="Plan name (its newest version) or id.")],
    project: Annotated[
        str | None, typer.Option(help="Project id, with a plan name (an API key implies it).")
    ] = None,
    api_url: Annotated[
        str, typer.Option(envvar=API_URL_ENV, help="RAI API base URL.")
    ] = "http://127.0.0.1:8000",
    trigger_ref: Annotated[
        str | None,
        typer.Option(envvar="GITHUB_SHA", help="What triggered the run, e.g. a commit SHA."),
    ] = None,
    idempotency_key: Annotated[
        str | None, typer.Option(help="Retrying with the same key returns the same run.")
    ] = None,
    wait: Annotated[bool, typer.Option(help="Wait for the verdict and gate on it.")] = True,
    release_gate: Annotated[
        bool, typer.Option(help="Also gate on the project's open critical findings (FR-12.2).")
    ] = True,
    timeout: Annotated[float, typer.Option(min=1, help="Seconds to wait for a verdict.")] = 1800,
    interval: Annotated[float, typer.Option(min=0.1, help="Seconds between polls.")] = 3,
) -> None:
    """Start a test plan and exit 0 if it and the release gate passed, 1 if either failed,
    2 if there is no verdict."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        typer.echo(f"ERROR set {API_KEY_ENV} to a project API key", err=True)
        raise typer.Exit(2)
    try:
        http = UrllibHttp(api_url, key)
    except RunProblem as problem:
        typer.echo(f"ERROR {problem}", err=True)
        raise typer.Exit(2) from None
    outcome = Gate(http, typer.echo).run(
        plan, project=project, trigger_ref=trigger_ref, idempotency_key=idempotency_key,
        wait=wait, timeout=timeout, interval=interval, release_gate=release_gate,
    )  # fmt: skip
    _github_outputs(outcome, http.base_url)
    raise typer.Exit(outcome.code)
