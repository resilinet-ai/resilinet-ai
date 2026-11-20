"""Protocol H3: RAI's RTO against the independent observer's ground truth (B-44).

Input: RAI's recovery measurements, one JSON object per line (``rai-bench-recovery/1``):

========== ===================================================================
injection  the injection id from the fault plan (B-28)
onset_at   the fault's onset as RAI recorded it (UTC, ISO 8601)
reverted_at the last revert
rto_seconds RAI's RTO, or null when RAI found it not restored within its window
window_seconds optional: RAI's recovery window, used for the ground truth too
========== ===================================================================

These are the fields of ``GET /v1/assurance/recovery`` (and ``recovery_measurements``).
The observer's log (``sandboxbank observe``) gives the ground truth for the same onset
and revert, by the same restoration rule (:func:`sandboxbank.observer.restoration`).

Per injection, RAI **agrees** with the truth when both found it restored and
``|RAI - truth| <= 2 s``, or when neither did. Injections the observer did not watch are
excluded and counted, never scored as agreement. H3 (Protocol section 7) is supported
when the share agreeing is at least 95% and its Clopper-Pearson 95% lower bound at
least 90%. Bland-Altman statistics describe the errors where both found a value.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from rai_bench.runner import HarnessError
from sandboxbank.observer import HOLD, Sample, read, restoration

FORMAT = "rai-bench-recovery/1"
TOLERANCE = 2.0  # seconds (Protocol H3)
POINT, LOWER = 0.95, 0.90  # supported if the share >= POINT and its lower bound >= LOWER


def _binomial_tail(k: int, n: int, p: float, upper: bool) -> float:
    """P(X >= k) if upper, else P(X <= k), for X ~ Binomial(n, p)."""
    if p <= 0:
        return 1.0 if (not upper or k <= 0) else 0.0
    if p >= 1:
        return 1.0 if (upper or k >= n) else 0.0
    terms = range(k, n + 1) if upper else range(0, k + 1)
    log_p, log_q = math.log(p), math.log1p(-p)
    return sum(
        math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                 + i * log_p + (n - i) * log_q)
        for i in terms
    )  # fmt: skip


def clopper_pearson(successes: int, trials: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided (1 - alpha) interval for a binomial proportion."""
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError("need 0 <= successes <= trials and trials > 0")

    def solve(target: float, upper_tail: bool, k: int) -> float:
        low, high = 0.0, 1.0
        for _ in range(80):  # bisection: far below float resolution
            mid = (low + high) / 2
            value = _binomial_tail(k, trials, mid, upper_tail)
            # P(X >= k) rises with p; P(X <= k) falls with p
            if (value < target) == upper_tail:
                low = mid
            else:
                high = mid
        return (low + high) / 2

    lower = 0.0 if successes == 0 else solve(alpha / 2, True, successes)
    upper = 1.0 if successes == trials else solve(alpha / 2, False, successes)
    return lower, upper


def bland_altman(errors: list[float]) -> dict[str, Any]:
    """Mean error (bias) and 95% limits of agreement, bias ± 1.96 SD."""
    n = len(errors)
    if n == 0:
        return {"n": 0, "bias": None, "sd": None, "lower_limit": None, "upper_limit": None}
    bias = sum(errors) / n
    sd = math.sqrt(sum((e - bias) ** 2 for e in errors) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "bias": round(bias, 3), "sd": round(sd, 3),
            "lower_limit": round(bias - 1.96 * sd, 3),
            "upper_limit": round(bias + 1.96 * sd, 3)}  # fmt: skip


def _when(value: Any, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise HarnessError(f"{where}: {value!r} is not an ISO 8601 time") from None
    if moment.tzinfo is None:
        raise HarnessError(f"{where}: {value!r} has no time zone")
    return moment


def read_measurements(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            where = f"{path.name} line {number}"
            try:
                row = json.loads(line)
            except ValueError:
                raise HarnessError(f"{where}: not JSON") from None
            if not isinstance(row, dict) or row.get("format") != FORMAT:
                raise HarnessError(f"{where}: not a {FORMAT} record")
            injection = row.get("injection")
            if not isinstance(injection, str) or not injection:
                raise HarnessError(f"{where}: no injection id")
            if injection in seen:
                raise HarnessError(f"{where}: injection {injection} appears twice")
            seen.add(injection)
            rto = row.get("rto_seconds")
            if rto is not None and (not isinstance(rto, int | float) or rto < 0):
                raise HarnessError(f"{where}: rto_seconds must be a number >= 0 or null")
            window = row.get("window_seconds")
            if window is not None and (not isinstance(window, int | float) or window <= 0):
                raise HarnessError(f"{where}: window_seconds must be a number > 0")
            rows.append({
                "injection": injection,
                "onset": _when(row.get("onset_at"), where),
                "reverted": _when(row.get("reverted_at"), where),
                "rto": None if rto is None else float(rto),
                "window": None if window is None else float(window),
            })  # fmt: skip
    return rows


def score_h3(
    measurements: list[dict[str, Any]], samples: list[Sample], *, hold: float = HOLD
) -> dict[str, Any]:
    """Per-injection comparison and the H3 summary."""
    results: list[dict[str, Any]] = []
    errors: list[float] = []
    agreed = scored = 0
    for row in measurements:
        try:
            truth = restoration(samples, row["onset"], row["reverted"], hold=hold,
                                window=row["window"])  # fmt: skip
        except ValueError as error:
            raise HarnessError(f"injection {row['injection']}: {error}") from None
        result: dict[str, Any] = {
            "injection": row["injection"], "rai_rto_seconds": row["rto"],
            "truth_rto_seconds": truth.seconds, "observed": truth.observed,
        }  # fmt: skip
        if not truth.observed:
            result.update(agrees=None, reason=truth.reason)
            results.append(result)
            continue
        scored += 1
        rai, true = row["rto"], truth.seconds
        if rai is not None and true is not None:
            diff = round(rai - true, 3)
            errors.append(diff)
            agrees = abs(diff) <= TOLERANCE
            result["error_seconds"] = diff
        else:
            agrees = rai is None and true is None
            result["reason"] = (
                "neither restored within the window" if agrees
                else ("RAI found it restored; the observer did not" if rai is not None
                      else "the observer found it restored; RAI did not")
            )  # fmt: skip
        agreed += agrees
        result["agrees"] = agrees
        results.append(result)
    summary: dict[str, Any] = {
        "injections": len(measurements), "scored": scored,
        "unobserved": len(measurements) - scored, "agreed": agreed,
        "tolerance_seconds": TOLERANCE, "hold_seconds": hold,
    }  # fmt: skip
    if scored:
        lower, upper = clopper_pearson(agreed, scored)
        share = agreed / scored
        summary.update(
            share=round(share, 4), ci95=[round(lower, 4), round(upper, 4)],
            supported=share >= POINT and lower >= LOWER,
        )  # fmt: skip
    else:
        summary.update(share=None, ci95=None, supported=False)
    summary["bland_altman"] = bland_altman(errors)
    return {"format": "rai-bench-h3/1", "summary": summary, "injections": results}


def h3(measurements: Path, observations: Path, *, hold: float = HOLD) -> dict[str, Any]:
    try:
        samples = read(observations)
    except (OSError, ValueError) as error:
        raise HarnessError(f"observation log: {error}") from None
    try:
        rows = read_measurements(measurements)
    except OSError as error:
        raise HarnessError(f"measurements: {error}") from None
    if not rows:
        raise HarnessError(f"{measurements} has no measurements")
    return score_h3(rows, samples, hold=hold)
