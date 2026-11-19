"""Every commit in a range is signed off under the DCO (NFR-LIC-1; CONTRIBUTING.md).

    python scripts/check_dco.py <base> <head>

A commit passes when it carries a ``Signed-off-by: Name <address>`` trailer whose
name is the commit author's. Names, not addresses, are compared: GitHub records a
squash merge's author with a no-reply address while the sign-off keeps the one the
contributor chose, and Dependabot signs off as ``support@github.com``. Merge commits
are skipped (they add no code of their own). Output names commits and authors only,
never e-mail addresses. Exit 0 if every commit is signed off, 1 if not, 2 on error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

SIGN_OFF = re.compile(r"^(?P<name>.+?)\s*<(?P<address>[^<>@\s]+@[^<>@\s]+)>$")
FIELD, RECORD, TRAILER = "\x00", "\x1e", "\x01"


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    author: str
    sign_offs: tuple[str, ...]


def missing(commit: Commit) -> str | None:
    """Why the commit fails the DCO check, or None if it passes."""
    names = []
    for trailer in commit.sign_offs:
        match = SIGN_OFF.match(trailer.strip())
        if match is not None:
            names.append(match["name"].strip().casefold())
    if not names:
        return "no Signed-off-by trailer with a name and an address"
    if commit.author.strip().casefold() not in names:
        return f"signed off, but not by its author {commit.author!r}"
    return None


def commits(base: str, head: str) -> list[Commit]:
    # git writes the separators itself (%x..): process arguments cannot hold a NUL.
    fmt = "%H%x00%an%x00%(trailers:key=Signed-off-by,valueonly,separator=%x01)%x1e"
    out = subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", "log", "--no-merges", f"--format={fmt}", f"{base}..{head}"],  # noqa: S607
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    found = []
    for record in out.split(RECORD):
        if not record.strip():
            continue
        sha, author, trailers = record.strip("\n").split(FIELD)
        found.append(Commit(sha, author, tuple(t for t in trailers.split(TRAILER) if t.strip())))
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_dco.py <base> <head>", file=sys.stderr)
        return 2
    try:
        found = commits(*argv)
    except subprocess.CalledProcessError as error:
        print(f"git log failed: {error.stderr.strip()}", file=sys.stderr)
        return 2
    failures = [(c, why) for c in found if (why := missing(c)) is not None]
    for commit, why in failures:
        print(f"{commit.sha[:10]} ({commit.author}): {why}", file=sys.stderr)
    if failures:
        print(
            f"{len(failures)} of {len(found)} commits are not signed off. "
            "Sign off with `git commit -s`; to fix earlier commits, "
            "`git rebase --signoff <base>` and push again.",
            file=sys.stderr,
        )
        return 1
    print(f"DCO: all {len(found)} commits signed off by their authors")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
