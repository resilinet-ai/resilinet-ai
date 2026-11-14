"""The reference environment recorded in every result file (Protocol section 9).

Paths and user names are never recorded: results are published.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import polars as pl

from rai import __version__ as rai_version
from rai_bench import __version__ as bench_version
from sandboxbank import __version__ as sandboxbank_version


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, *args], cwd=root, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def environment(root: Path) -> dict[str, Any]:
    """Machine, software versions, lockfile hash and source revision."""
    lockfile = root / "uv.lock"
    status = _git(root, "status", "--porcelain")
    return {
        "os": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "versions": {
            "rai": rai_version,
            "rai-bench": bench_version,
            "rai-sandboxbank": sandboxbank_version,
            "polars": pl.__version__,
        },
        "lockfile_sha256": (
            hashlib.sha256(lockfile.read_bytes()).hexdigest() if lockfile.is_file() else None
        ),
        "git_commit": _git(root, "rev-parse", "HEAD"),
        "git_dirty": None if status is None else bool(status),
    }
