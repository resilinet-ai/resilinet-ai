"""Reproduce a RAI release on this machine and write the report (B-41: NFR-REP-2).

    make reproduce                       # or: uv run python scripts/reproduce.py [--out FILE]

On a clean checkout of a release tag, this runs what NFR-REP-2 asks of the release:

1. ``uv sync --all-packages --locked``: exactly the locked packages;
2. ``make demo``: the first runnable slice, whose report must be byte-identical twice;
3. ``make journeys``: the whole stack (rai-db, SandboxBank, the chaos agent, the API and
   a worker) and journeys J1-J5 end to end, including a real chaos fault.

While the journeys run, it samples the containers' memory with ``docker stats``. The
report records the machine (memory, CPUs, operating system), the tool versions, the
commit and tag, each step's result and time, and the containers' peak memory. It is
the evidence that goes with the release: ``docs/releasing.md``.

Exit code 0 only if every step passed. An "8 GB machine" is one whose physical memory
is at most 8 GiB, so the same run on a larger machine still passes but says it is not
the 8 GB reproduction.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3
EIGHT_GB = 8 * GIB
UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": GIB,
}


@dataclass
class Step:
    name: str
    command: list[str]
    seconds: float = 0.0
    exit_code: int | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass
class Machine:
    os: str
    cpus: int
    memory_bytes: int | None
    docker_memory_bytes: int | None
    tools: dict[str, str] = field(default_factory=dict)

    @property
    def eight_gb(self) -> bool:
        return self.memory_bytes is not None and self.memory_bytes <= EIGHT_GB


def parse_size(text: str) -> int:
    """``docker stats`` sizes such as ``512MiB`` or ``1.2GB``, in bytes."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", text)
    if match is None or match.group(2) not in UNITS:
        raise ValueError(f"not a size: {text!r}")
    return round(float(match.group(1)) * UNITS[match.group(2)])


def containers_in_use(lines: str) -> int:
    """The total of ``docker stats --format {{.MemUsage}}`` lines (``used / limit``)."""
    return sum(parse_size(line.split("/")[0]) for line in lines.splitlines() if "/" in line)


def physical_memory() -> int | None:
    if sys.platform == "win32":

        class Status(ctypes.Structure):
            _fields_ = [  # the ctypes layout of MEMORYSTATUSEX
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
        return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def output(*command: str) -> str:
    """The first line a tool prints, or why it could not run."""
    if shutil.which(command[0]) is None:
        return "not found"
    done = subprocess.run(command, capture_output=True, text=True, check=False, cwd=ROOT)  # noqa: S603
    text = (done.stdout or done.stderr).strip()
    return text.splitlines()[0] if text else f"exit code {done.returncode}"


def machine() -> Machine:
    docker_memory = output("docker", "info", "--format", "{{.MemTotal}}")
    return Machine(
        os=platform.platform(),
        cpus=os.cpu_count() or 0,
        memory_bytes=physical_memory(),
        docker_memory_bytes=int(docker_memory) if docker_memory.isdigit() else None,
        tools={
            "git": output("git", "--version"),
            "uv": output("uv", "--version"),
            "make": output("make", "--version"),
            "docker": output("docker", "--version"),
            "docker compose": output("docker", "compose", "version"),
        },
    )


class MemorySampler:
    """The peak of all containers' memory together, sampled while a step runs."""

    def __init__(self, every: float = 2.0) -> None:
        self.every = every
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            stats = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            if stats.returncode == 0:
                with contextlib.suppress(ValueError):  # a line docker could not measure
                    self.peak = max(self.peak, containers_in_use(stats.stdout))
            self._stop.wait(self.every)

    def __enter__(self) -> MemorySampler:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()


def run(step: Step) -> Step:
    print(f"\n=== {step.name}: {' '.join(step.command)}", flush=True)
    start = time.monotonic()
    try:
        step.exit_code = subprocess.run(step.command, cwd=ROOT, check=False).returncode  # noqa: S603
    except FileNotFoundError:
        step.exit_code = 127
    step.seconds = time.monotonic() - start
    return step


def gib(value: int | None) -> str:
    return "unknown" if value is None else f"{value / GIB:.1f} GiB"


def report(
    when: datetime,
    commit: str,
    tag: str,
    clean: bool,
    host: Machine,
    steps: Sequence[Step],
    peak: int,
) -> str:
    passed = all(s.passed for s in steps)
    lines = [
        f"# Reproduction of RAI {tag}",
        "",
        f"- **Result:** {'PASS' if passed else 'FAIL'}",
        f"- **When:** {when.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Commit:** `{commit}`, tag `{tag}`, working tree "
        + ("clean" if clean else "**not clean**"),
        f"- **Machine:** {host.os}, {host.cpus} CPUs, {gib(host.memory_bytes)} of memory "
        + (
            "(an 8 GB machine)"
            if host.eight_gb
            else "(**not** an 8 GB machine: this is not the 8 GB reproduction)"
        ),
        f"- **Docker's memory:** {gib(host.docker_memory_bytes)}",
        f"- **Containers' peak memory during the journeys:** {gib(peak)} (the API, the worker"
        " and the tests run on the host and are not included)",
        "",
        "| Step | Command | Result | Time |",
        "| --- | --- | --- | --- |",
    ]
    for step in steps:
        result = "pass" if step.passed else f"FAIL (exit code {step.exit_code})"
        command = " ".join(step.command)
        lines.append(f"| {step.name} | `{command}` | {result} | {step.seconds:.0f} s |")
    lines += ["", "| Tool | Version |", "| --- | --- |"]
    lines += [f"| {name} | {version} |" for name, version in host.tools.items()]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, help="where to write the report (Markdown)")
    args = parser.parse_args(argv)

    when = datetime.now(UTC)
    commit = output("git", "rev-parse", "HEAD")
    tag = output("git", "describe", "--tags", "--exact-match")
    tag = tag if re.fullmatch(r"v[\w.-]+", tag) else "untagged"
    clean = output("git", "status", "--porcelain") == "exit code 0"  # it printed nothing
    host = machine()

    steps = [run(Step("Install the locked packages", ["uv", "sync", "--all-packages", "--locked"]))]
    if steps[0].passed:
        steps.append(run(Step("First runnable slice", ["make", "demo"])))
        with MemorySampler() as sampler:
            steps.append(run(Step("Journeys J1-J5 on the whole stack", ["make", "journeys"])))
        peak = sampler.peak
    else:
        peak = 0

    text = report(when, commit, tag, clean, host, steps, peak)
    out = args.out or ROOT / f"reproduction-{tag}.md"
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"\n{text}\nThe report is in {out}")
    return 0 if all(s.passed for s in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
