"""``rai run``: exit codes, reporting and GitHub outputs (B-20: FR-06.1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rai.cli.main import app
from rai.cli.run import Gate, Reply, RunProblem, UrllibHttp

RUN = "0193a5b0-0000-7000-8000-000000000001"


def run_body(state: str, *, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    steps = (
        [{"step_key": "item-1", "state": "done", "verdict": "fail"}] if state == "failed" else []
    )
    return {
        "run_id": RUN,
        "state": state,
        "verdict": None,
        "reason": reason,
        "steps": steps,
    } | extra


class Script:
    """A fake API: answers in order, records the requests."""

    def __init__(self, *replies: Reply) -> None:
        self.replies: Iterator[Reply] = iter(replies)
        self.sent: list[tuple[str, str, Any, dict[str, str]]] = []

    def request(self, method: str, path: str, *, body: Any = None,
                headers: dict[str, str] | None = None) -> Reply:  # fmt: skip
        self.sent.append((method, path, body, headers or {}))
        return next(self.replies)


def gate(script: Script, lines: list[str]) -> Gate:
    clock = iter(range(0, 10_000, 5))
    return Gate(script, lines.append, clock=lambda: float(next(clock)), sleep=lambda _: None)


FAILING = {"items": [{
    "step_key": "item-1", "rule": "unique_across_batches:txn_id", "outcome": "fail",
    "row_count": 2002, "failing_count": 2, "expected": {"severity": "critical"},
    "observed_value": {"message": "2 rows repeat earlier batches"},
}], "next_cursor": None}  # fmt: skip


def test_a_passing_run_exits_0_and_a_plan_name_is_sent_by_name() -> None:
    lines: list[str] = []
    script = Script(Reply(202, run_body("queued")), Reply(200, run_body("running")),
                    Reply(200, run_body("passed")))  # fmt: skip
    outcome = gate(script, lines).run("ci-contracts", trigger_ref="abc123", idempotency_key="k1")
    assert outcome.code == 0
    method, path, body, headers = script.sent[0]
    assert (method, path) == ("POST", "/v1/assurance/runs")
    assert body == {"plan": "ci-contracts", "trigger_ref": "abc123"}
    assert headers == {"Idempotency-Key": "k1"}
    assert lines[0] == f"run {RUN} queued (plan ci-contracts @ abc123)"
    assert lines[-1] == f"PASSED run {RUN}"


def test_a_failing_run_exits_1_and_names_the_failing_rules() -> None:
    lines: list[str] = []
    script = Script(Reply(202, run_body("queued")), Reply(200, run_body("failed")),
                    Reply(200, FAILING), Reply(200, {"items": []}))  # fmt: skip
    outcome = gate(script, lines).run("0193a5b0-0000-7000-8000-0000000000aa", project="ignored")
    assert outcome.code == 1
    assert script.sent[0][2] == {"plan_id": "0193a5b0-0000-7000-8000-0000000000aa",
                                 "trigger_ref": None}  # fmt: skip
    assert lines[-2:] == [
        f"FAILED run {RUN}: item-1 fail",
        "  FAIL  item-1 unique_across_batches:txn_id (critical) 2/2,002 rows "
        "2 rows repeat earlier batches",
    ]


@pytest.mark.parametrize("state", ["refused", "error", "aborted"])
def test_a_run_without_a_verdict_exits_2(state: str) -> None:
    lines: list[str] = []
    script = Script(Reply(202, run_body("queued")),
                    Reply(200, run_body(state, reason="connector 'feed' is not on this worker")),
                    Reply(200, {"items": []}), Reply(200, {"items": []}))  # fmt: skip
    assert gate(script, lines).run("p").code == 2
    assert "  reason: connector 'feed' is not on this worker" in lines


def test_no_wait_only_queues() -> None:
    script = Script(Reply(202, run_body("queued")))
    assert gate(script, []).run("p", wait=False).code == 0
    assert len(script.sent) == 1


def test_api_problems_exit_2_with_the_reason() -> None:
    lines: list[str] = []
    missing = Reply(404, {"title": "Plan not found", "code": "plans.not_found"})
    assert gate(Script(missing), lines).run("nope").code == 2
    assert lines == ["ERROR could not start plan 'nope': Plan not found"]
    invalid = Reply(422, {"title": "Invalid request", "errors": [{"message": "bad"}]})
    assert gate(Script(invalid), lines).run("p").code == 2
    assert lines[-1] == "ERROR could not start plan 'p': Invalid request (bad)"


def test_waiting_tolerates_brief_outages_but_not_forever() -> None:
    lines: list[str] = []
    flaky = Script(Reply(202, run_body("queued")), Reply(503, None), Reply(429, None),
                   Reply(200, run_body("passed")))  # fmt: skip
    assert gate(flaky, lines).run("p").code == 0
    down = Script(Reply(202, run_body("queued")), *[Reply(502, None)] * 6)
    assert gate(down, lines).run("p").code == 2
    assert lines[-1] == f"ERROR could not follow run {RUN}: HTTP 502"
    gone = Script(Reply(202, run_body("queued")), Reply(401, {"title": "Sign in first"}))
    assert gate(gone, lines).run("p").code == 2


def test_a_run_that_outlasts_the_timeout_exits_2() -> None:
    lines: list[str] = []
    slow = Script(Reply(202, run_body("queued")), *[Reply(200, run_body("running"))] * 10)
    assert gate(slow, lines).run("p", timeout=12).code == 2
    assert lines[-1] == f"ERROR run {RUN} has no verdict after 12 s; it keeps running"


def test_keys_are_never_sent_in_clear_over_a_network() -> None:
    with pytest.raises(RunProblem, match="must use https"):
        UrllibHttp("http://rai.example.test", "k")
    with pytest.raises(RunProblem, match="http"):
        UrllibHttp("file:///etc/passwd", "k")
    assert UrllibHttp("http://127.0.0.1:8000/", "k").base_url == "http://127.0.0.1:8000"
    assert "k" not in repr(UrllibHttp("https://rai.example.test", "k")).split("base_url")[0]


def test_an_unreachable_api_is_a_clear_error() -> None:
    http = UrllibHttp("http://127.0.0.1:9", "k", timeout=2)  # discard port: nothing listens
    with pytest.raises(RunProblem, match="could not reach the RAI API"):
        http.request("GET", "/healthz")


def test_the_command_needs_a_key_and_writes_github_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.delenv("RAI_API_KEY", raising=False)
    result = runner.invoke(app, ["run", "--plan", "p"])
    assert result.exit_code == 2
    assert "set RAI_API_KEY" in result.output
    monkeypatch.setenv("RAI_API_KEY", "rai_x")
    result = runner.invoke(app, ["run", "--plan", "p", "--api-url", "http://rai.example.test"])
    assert result.exit_code == 2

    output, summary = tmp_path / "out", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    script = Script(Reply(202, run_body("queued")), Reply(200, run_body("failed")),
                    Reply(200, FAILING), Reply(200, {"items": []}))  # fmt: skip
    monkeypatch.setattr(
        "rai.cli.run.UrllibHttp.request", lambda self, *a, **k: script.request(*a, **k)
    )
    result = runner.invoke(app, ["run", "--plan", "p", "--interval", "0.1"])
    assert result.exit_code == 1, result.output
    assert output.read_text("utf-8") == f"run-id={RUN}\nstate=failed\nverdict=\n"
    text = summary.read_text("utf-8")
    assert "### RAI gate: FAILED" in text
    assert "| item-1 | `unique_across_batches:txn_id` | fail | 2 |" in text
