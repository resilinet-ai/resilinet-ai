"""The baselines' failure-scenario halves, and how alerts are matched to injections
(Protocol sections 3 and 8; the author's decisions of 2026-11-25).

During the chaos benchmark the baselines watch the live system as a team would:

* **B1** reruns its integration tests every 60 s (``benchmarks/baselines/b1/test_live.py``,
  read-only). Each failing test is one alert naming the component it checks.
* **B2** is a 1-second health monitor of **liveness** only: payments-api ``/readyz``,
  and ``/healthz`` of the ETL and the feature-job (never their freshness ``/readyz``).
  It raises one alert when a component goes from healthy to failing, as monitoring
  pages someone once per outage.

**Components.** Alerts name components: ``payments-api``, ``ledger-db``, ``analytics-db``,
``etl-job``, ``feature-job``, ``model-api``. An injection affects the components of its
blast radius and of the probes its recovery depends on (:data:`COMPONENT_OF` maps the
registry's allow-listed target names onto them).

**Matching rule** (section 8): an alert matches an injection when it was raised between
the fault's onset and five minutes after the observer's restoration, and names a
component the injection affects. Detection time is the first matching alert minus the
onset. Any other alert raised during a run is a false alarm.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rai_bench.alerts import Alert

ROOT = Path(__file__).resolve().parents[3]
B1_LIVE = ROOT / "benchmarks" / "baselines" / "b1" / "test_live.py"
B1_INTERVAL = 60.0
B2_INTERVAL = 1.0
PROBE_TIMEOUT = 2.0
AFTER_RESTORATION = timedelta(minutes=5)

# Allow-listed target names (the registry's vocabulary) -> the component they belong to.
COMPONENT_OF: dict[str, str] = {
    "payments-api": "payments-api", "payments-health": "payments-api",
    "payments-ledger": "ledger-db", "ledger-db": "ledger-db", "ledger-db-proxy": "ledger-db",
    "analytics-db-proxy": "analytics-db", "etl-job": "etl-job", "etl-ready": "etl-job",
    "feature-job": "feature-job", "features-ready": "feature-job",
    "model-api-proxy": "model-api", "model-api": "model-api",
}  # fmt: skip
# B1's live tests -> the component each one checks.
B1_COMPONENTS: dict[str, str] = {
    "test_payments_api_is_ready": "payments-api",
    "test_ledger_head_is_readable_and_never_falls": "ledger-db",
    "test_warehouse_answers": "analytics-db",
    "test_warehouse_keeps_up_with_the_ledger": "etl-job",
    "test_feature_table_keeps_up_with_the_warehouse": "feature-job",
}
LIVE_SUBJECT = "live"  # alerts are raised about the running system; matching assigns them


@dataclass(frozen=True)
class Injection:
    """What matching needs to know about one injection."""

    injection_id: str
    onset: datetime
    restored: datetime | None  # the observer's ground truth; None: never restored
    window_end: datetime  # the last revert plus the recovery window: used when not restored
    components: frozenset[str]

    @property
    def closes(self) -> datetime:
        return (self.restored or self.window_end) + AFTER_RESTORATION


def affected(
    blast_radius: Iterable[str], probes: Iterable[str], ledger: str | None
) -> frozenset[str]:
    """The components an injection affects: its blast radius and its recovery probes."""
    names = [*blast_radius, *probes, *([ledger] if ledger else [])]
    unknown = sorted(n for n in names if n not in COMPONENT_OF)
    if unknown:
        raise ValueError(f"no component for target(s) {unknown}")
    return frozenset(COMPONENT_OF[n] for n in names)


def matches(alert: Alert, injection: Injection) -> bool:
    return injection.onset <= alert.time <= injection.closes and bool(
        set(alert.location) & injection.components
    )


def assign(alerts: Iterable[Alert], injections: list[Injection]) -> dict[str, Any]:
    """Per injection, the matching alerts of each arm; every other alert is a false alarm."""
    per: dict[str, dict[str, list[Alert]]] = {i.injection_id: {} for i in injections}
    false_alarms: list[Alert] = []
    for alert in alerts:
        hits = [i for i in injections if matches(alert, i)]
        for injection in hits:
            per[injection.injection_id].setdefault(alert.arm, []).append(alert)
        if not hits:
            false_alarms.append(alert)
    return {"matched": per, "false_alarms": false_alarms}


def detection_seconds(injection: Injection, alerts: list[Alert]) -> float | None:
    if not alerts:
        return None
    return round((min(a.time for a in alerts) - injection.onset).total_seconds(), 3)


def _alert(arm: str, component: str, message: str, moment: datetime) -> Alert:
    return Alert(arm=arm, subject_kind="injection", subject_id=LIVE_SUBJECT, time=moment,
                 location=(component,), message=message)  # fmt: skip


# ---- B2: the 1-second liveness monitor --------------------------------------------------------


def http_status(url: str, timeout: float = PROBE_TIMEOUT) -> int:
    """The HTTP status, or 0 when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return 0


class B2Monitor:
    """Polls each component's liveness endpoint every second; one alert per outage."""

    def __init__(
        self,
        endpoints: Mapping[str, str],
        emit: Callable[[Alert], None],
        *,
        interval: float = B2_INTERVAL,
        status: Callable[[str], int] = http_status,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.endpoints, self.emit, self.interval = dict(endpoints), emit, interval
        self._status, self._clock = status, clock
        self._failing: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        for component, url in self.endpoints.items():
            code = self._status(url)
            healthy = 200 <= code < 300
            if not healthy and component not in self._failing:
                self._failing.add(component)
                what = "did not answer" if code == 0 else f"answered {code}"
                self.emit(_alert("B2", component, f"{component} health check {what}",
                                 self._clock()))  # fmt: skip
            elif healthy:
                self._failing.discard(component)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def b2_endpoints(payments: str, etl: str, features: str) -> dict[str, str]:
    """B2's liveness endpoints (the author's decision: liveness only)."""
    return {"payments-api": payments.rstrip("/") + "/readyz",
            "etl-job": etl.rstrip("/") + "/healthz",
            "feature-job": features.rstrip("/") + "/healthz"}  # fmt: skip


# ---- B1: the integration tests, every 60 s ---------------------------------------------------


def b1_alerts(junit: Path, moment: datetime) -> list[Alert]:
    """One alert per failing live test, naming its component."""
    alerts = []
    for case in ET.parse(junit).getroot().iter("testcase"):  # noqa: S314 - our own pytest output
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        name = case.get("name", "")
        component = B1_COMPONENTS.get(name)
        if component is None:
            continue
        detail = (failure.get("message") or "").splitlines()[0].removeprefix("AssertionError: ")
        alerts.append(_alert("B1", component, f"{name}: {detail}", moment))
    return alerts


class B1Live:
    """Runs B1's live tests every 60 s in their own pytest process, as a CI job would."""

    def __init__(
        self,
        workdir: Path,
        env: Mapping[str, str],
        emit: Callable[[Alert], None],
        *,
        interval: float = B1_INTERVAL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        tests: Path = B1_LIVE,
    ) -> None:
        self.workdir, self.env, self.emit, self.interval = workdir, dict(env), emit, interval
        self._clock, self._tests = clock, tests
        self._runs = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[Alert]:
        self._runs += 1
        junit = self.workdir / f"b1-live-{self._runs:05d}.xml"
        env = os.environ | self.env | {"RAI_B1_STATE": str(self.workdir / "b1-live-state.json")}
        moment = self._clock()
        done = subprocess.run(  # noqa: S603 - a fixed command: this interpreter running pytest
            [sys.executable, "-m", "pytest", str(self._tests), "-c",
             str(self._tests.parent / "pytest.ini"), "--rootdir", str(self._tests.parent),
             f"--junitxml={junit}", "-p", "no:cacheprovider", "-q"],
            env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if done.returncode not in (0, 1) or not junit.is_file():
            raise RuntimeError(f"B1 live suite could not run (exit {done.returncode}): "
                               f"{done.stdout[-400:]}")  # fmt: skip
        alerts = b1_alerts(junit, moment)
        for alert in alerts:
            self.emit(alert)
        return alerts

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except RuntimeError as error:  # the suite could not run: noted, never an alert
                with (self.workdir / "b1-live-errors.log").open("a", encoding="utf-8") as log:
                    log.write(f"{self._clock().isoformat()} {error}\n")
            self._stop.wait(self.interval)

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
