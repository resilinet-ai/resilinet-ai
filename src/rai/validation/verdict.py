"""Severity, thresholds and the run verdict (B-12, FR-03.4).

Each rule has already been judged against its own threshold by the engine
(``pass`` means the failing rows were within tolerance). This module turns those
per-rule outcomes into one verdict:

* A rule **blocks** when its outcome is ``fail`` or ``error`` and its severity is
  at or above ``fail_on`` (default ``critical``). Any blocking rule fails the
  batch. An ``error`` blocks too: a critical rule that could not be evaluated
  must never pass a batch.
* ``fail`` or ``error`` below ``fail_on`` is a **warning**: reported, not fatal.
* Severity order: ``info`` < ``warn`` < ``critical``. ``fail_on="warn"`` makes
  warnings block as well, for stricter CI gates.
* A run made of several batches or contracts fails if any of them fails.

Waivers with expiry (FR-12.2) arrive with B-33 and will act on blocking rules.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from rai.validation.contract import Severity
from rai.validation.engine import RuleResult, ValidationReport

VerdictOutcome = Literal["pass", "fail"]
SEVERITY_RANK: dict[Severity, int] = {"info": 0, "warn": 1, "critical": 2}
PROBLEM_OUTCOMES = frozenset({"fail", "error"})


@dataclass(frozen=True, slots=True)
class Verdict:
    outcome: VerdictOutcome
    fail_on: Severity
    blocking: tuple[RuleResult, ...]
    warnings: tuple[RuleResult, ...]
    counts: dict[str, int]  # rules by outcome: pass, fail, error, skipped
    subject: str = ""  # e.g. "transactions_raw@1 B000042"

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @property
    def exit_code(self) -> int:
        """0 when the batch passes, 1 when it fails (CLI and CI gates)."""
        return 0 if self.passed else 1

    def summary(self) -> str:
        head = ("PASS" if self.passed else "FAIL") + (f" {self.subject}" if self.subject else "")
        details = []
        if self.blocking:
            ids = ", ".join(r.rule_id for r in self.blocking)
            details.append(f"{len(self.blocking)} blocking ({ids})")
        if self.warnings:
            ids = ", ".join(r.rule_id for r in self.warnings)
            noun = "warning" if len(self.warnings) == 1 else "warnings"
            details.append(f"{len(self.warnings)} {noun} ({ids})")
        return head + (" - " + "; ".join(details) if details else "")

    def to_dict(self) -> dict[str, Any]:
        def brief(result: RuleResult) -> dict[str, Any]:
            return {
                "rule_id": result.rule_id,
                "severity": result.severity,
                "outcome": result.outcome,
                "failing_count": result.failing_count,
                "row_count": result.row_count,
                "threshold": result.threshold,
                "message": result.message,
            }

        return {
            "subject": self.subject,
            "verdict": self.outcome,
            "fail_on": self.fail_on,
            "counts": dict(sorted(self.counts.items())),
            "blocking": [brief(r) for r in self.blocking],
            "warnings": [brief(r) for r in self.warnings],
        }


def decide(
    report: ValidationReport, *, fail_on: Severity = "critical", subject: str = ""
) -> Verdict:
    """The verdict for one validated batch."""
    if fail_on not in SEVERITY_RANK:
        raise ValueError(f"fail_on must be one of {', '.join(SEVERITY_RANK)}")
    bar = SEVERITY_RANK[fail_on]
    problems = [r for r in report.results if r.outcome in PROBLEM_OUTCOMES]
    blocking = tuple(r for r in problems if SEVERITY_RANK[r.severity] >= bar)
    warnings = tuple(r for r in problems if SEVERITY_RANK[r.severity] < bar)
    counts = Counter({"pass": 0, "fail": 0, "error": 0, "skipped": 0})
    counts.update(r.outcome for r in report.results)
    return Verdict(
        outcome="fail" if blocking else "pass",
        fail_on=fail_on,
        blocking=blocking,
        warnings=warnings,
        counts=dict(counts),
        subject=subject or report.contract_ref,
    )


@dataclass(frozen=True, slots=True)
class RunVerdict:
    outcome: VerdictOutcome
    verdicts: tuple[Verdict, ...]

    @property
    def failed(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.passed)

    @property
    def exit_code(self) -> int:
        return 0 if self.outcome == "pass" else 1

    def summary(self) -> str:
        total, failed = len(self.verdicts), len(self.failed)
        head = "PASS" if self.outcome == "pass" else "FAIL"
        return f"{head}: {total - failed} of {total} passed" + (
            f"; failed: {', '.join(v.subject for v in self.failed)}" if failed else ""
        )


def combine(verdicts: Iterable[Verdict]) -> RunVerdict:
    """A run fails if any of its batches or contracts fails. An empty run cannot pass."""
    collected = tuple(verdicts)
    if not collected:
        raise ValueError("a run needs at least one verdict")
    outcome: VerdictOutcome = "pass" if all(v.passed for v in collected) else "fail"
    return RunVerdict(outcome, collected)
