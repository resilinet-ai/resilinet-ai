"""Apache-2.0 everywhere, and every commit signed off under the DCO (NFR-LIC-1)."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.check_dco import Commit, commits, main, missing

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ["pyproject.toml", "chaos_agent/pyproject.toml", "sandboxbank/pyproject.toml",
            "benchmarks/pyproject.toml"]  # fmt: skip


# ---- the licence -----------------------------------------------------------------


def test_the_licence_is_apache_2_with_a_notice() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert (ROOT / "NOTICE").read_text(encoding="utf-8").strip()


@pytest.mark.parametrize("path", PACKAGES)
def test_every_python_package_declares_apache_2(path: str) -> None:
    project = tomllib.loads((ROOT / path).read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"


def test_the_web_ui_declares_apache_2() -> None:
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert package["license"] == "Apache-2.0"
    assert package.get("private") is True  # never published to npm by accident


def test_contributing_asks_for_the_dco() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Developer Certificate of Origin" in text
    assert "git commit -s" in text


def test_ci_checks_the_dco_on_every_pull_request() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python scripts/check_dco.py" in ci
    assert "fetch-depth: 0" in ci  # the whole range of the pull request


# ---- the DCO check ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("author", "trailers", "problem"),
    [
        ("Ada Lovelace", ("Ada Lovelace <ada@example.test>",), None),
        ("ada lovelace", ("Ada Lovelace <other@example.test>",), None),  # name, not address
        ("dependabot[bot]", ("dependabot[bot] <support@github.com>",), None),
        ("Ada Lovelace", ("Someone <s@example.test>", "Ada Lovelace <a@example.test>"), None),
        ("Ada Lovelace", (), "no Signed-off-by"),
        ("Ada Lovelace", ("Ada Lovelace",), "no Signed-off-by"),  # no address
        ("Ada Lovelace", ("Ada Lovelace <not-an-address>",), "no Signed-off-by"),
        ("Ada Lovelace", ("Charles Babbage <c@example.test>",), "not by its author"),
    ],
)
def test_a_commit_needs_its_authors_sign_off(
    author: str, trailers: tuple[str, ...], problem: str | None
) -> None:
    why = missing(Commit("abc", author, trailers))
    assert (why is None) if problem is None else (why is not None and problem in why)


def git(repo: Path, *args: str) -> str:
    person = {"NAME": "Ada Lovelace", "EMAIL": "ada@example.test"}
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo), "PATH": os.environ["PATH"]}
    env |= {f"GIT_{role}_{k}": v for role in ("AUTHOR", "COMMITTER") for k, v in person.items()}
    return subprocess.run(  # noqa: S603
        ["git", "-c", "commit.gpgsign=false", *args], cwd=repo, env=env,  # noqa: S607
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def test_the_range_is_read_from_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "-q", "--allow-empty", "-s", "-m", "feat: signed")
    signed = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "feat: not signed\n\nSome body.")
    monkeypatch.chdir(tmp_path)

    found = commits(base, "HEAD")
    assert [c.author for c in found] == ["Ada Lovelace", "Ada Lovelace"]
    assert [missing(c) is None for c in found] == [False, True]  # newest first

    assert main([base, signed]) == 0
    assert main([base, "HEAD"]) == 1
    err = capsys.readouterr().err
    assert "1 of 2 commits are not signed off" in err
    assert "@" not in err  # never prints an address
    assert main(["no-such-ref", "HEAD"]) == 2
    assert main([]) == 2
