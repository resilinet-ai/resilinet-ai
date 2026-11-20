"""Release versions and their checks (B-41: NFR-REP-2). Standard library only.

    python scripts/release.py bump 1.0.0     # set every version, locks included
    python scripts/release.py check v1.0.0   # may this tag be released?

One version for the whole repository: the four Python packages, their entries in
``uv.lock``, and the web UI's ``package.json`` and ``package-lock.json``. ``check`` is
what the workflow "Release" runs first on a pushed tag; it fails when any version
differs from the tag, when the tag's release notes are missing, or when they are still
marked as a draft. ``docs/releasing.md`` is the whole procedure.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECTS = {
    "resilinet-ai": "pyproject.toml",
    "rai-bench": "benchmarks/pyproject.toml",
    "rai-chaos-agent": "chaos_agent/pyproject.toml",
    "rai-sandboxbank": "sandboxbank/pyproject.toml",
}
PACKAGE_JSON = "web/package.json"
PACKAGE_LOCK = "web/package-lock.json"
TAG = re.compile(r"v(\d+\.\d+\.\d+)")
DRAFT = "Status: draft"  # the notes say this until the release is ready to tag


def notes_path(tag: str) -> str:
    return f"docs/releases/{tag}.md"


def _lock_entry(name: str) -> re.Pattern[str]:
    return re.compile(rf'(\[\[package\]\]\nname = "{re.escape(name)}"\nversion = ")([^"]+)(")')


def versions(root: Path = ROOT) -> dict[str, str]:
    """Every place that states the repository's version, by where it is."""
    found: dict[str, str] = {}
    for rel in PYPROJECTS.values():
        project = tomllib.loads((root / rel).read_text(encoding="utf-8"))["project"]
        found[rel] = project["version"]
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    for name in PYPROJECTS:
        match = _lock_entry(name).search(lock)
        found[f"uv.lock ({name})"] = match.group(2) if match else "missing"
    found[PACKAGE_JSON] = json.loads((root / PACKAGE_JSON).read_text(encoding="utf-8"))["version"]
    npm_lock = json.loads((root / PACKAGE_LOCK).read_text(encoding="utf-8"))
    found[PACKAGE_LOCK] = npm_lock["version"]
    found[f"{PACKAGE_LOCK} (packages)"] = npm_lock["packages"][""]["version"]
    return found


def check(tag: str, root: Path = ROOT) -> list[str]:
    """What stops ``tag`` from being released; empty when nothing does."""
    match = TAG.fullmatch(tag)
    if match is None:
        return [f"{tag}: a release tag is vMAJOR.MINOR.PATCH, for example v1.0.0"]
    version = match.group(1)
    problems = [
        f"{where} says {found}, the tag says {version} (run: python scripts/release.py "
        f"bump {version})"
        for where, found in versions(root).items()
        if found != version
    ]
    notes = root / notes_path(tag)
    if not notes.is_file():
        problems.append(f"{notes_path(tag)} is missing: the release notes come from it")
    elif DRAFT in notes.read_text(encoding="utf-8"):
        problems.append(f'{notes_path(tag)} still says "{DRAFT}"')
    return problems


def _replace(path: Path, pattern: re.Pattern[str], version: str, *, count: int) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = pattern.subn(rf"\g<1>{version}\g<3>", text, count=count)
    if n != count:
        raise ValueError(f"{path}: expected {count} version line(s), found {n}")
    path.write_text(new, encoding="utf-8", newline="\n")


def bump(version: str, root: Path = ROOT) -> None:
    """Set every version to ``version``, keeping each file's formatting."""
    if TAG.fullmatch(f"v{version}") is None:
        raise ValueError(f"{version}: a version is MAJOR.MINOR.PATCH, for example 1.0.0")
    project_version = re.compile(r'(?m)(^\[project\]\n(?:(?!\[).*\n)*?version = ")([^"]+)(")')
    for rel in PYPROJECTS.values():
        _replace(root / rel, project_version, version, count=1)
    for name in PYPROJECTS:
        _replace(root / "uv.lock", _lock_entry(name), version, count=1)
    _replace(root / PACKAGE_JSON, re.compile(r'(?m)(^  "version": ")([^"]+)(")'), version, count=1)
    # The lock states the UI's own version twice: at the top and under packages[""].
    lock = root / PACKAGE_LOCK
    _replace(lock, re.compile(r'(?m)(^ {2}"version": ")([^"]+)(")'), version, count=1)
    own = re.compile(r'(?s)("packages": \{\n {4}"": \{\n[^}]*?"version": ")([^"]+)(")')
    _replace(lock, own, version, count=1)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"bump", "check"}:
        print(__doc__, file=sys.stderr)
        return 2
    command, value = argv
    if command == "bump":
        bump(value)
        print(f"every version is now {value}")
        return 0
    problems = check(value)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print(f"{value} may be released")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
