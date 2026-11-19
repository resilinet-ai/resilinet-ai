"""The documentation site: its link hook and its navigation (FR-14.1).

CI's "Docs site" job builds the site with ``mkdocs build --strict``; these tests cover
the hook that keeps links leaving ``docs/`` working, and the site's shape, without
needing MkDocs installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.docs_links import REPO_URL, outside_url, rewrite

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("page", "target", "expected"),
    [
        ("quickstart.md", "api.md", None),  # a page of the site
        ("playbooks/triage.md", "../api.md#findings", None),
        ("quickstart.md", "ui/dashboard.png", None),  # a file of the site
        ("quickstart.md", "https://docs.astral.sh/uv/", None),
        ("quickstart.md", "#clean-up", None),
        ("quickstart.md", "../contracts/README.md", f"{REPO_URL}/blob/main/contracts/README.md"),
        ("playbooks/index.md", "../../sandboxbank/README.md#seeding-log",
         f"{REPO_URL}/blob/main/sandboxbank/README.md#seeding-log"),
        ("index.md", "../CONTRIBUTING.md", f"{REPO_URL}/blob/main/CONTRIBUTING.md"),
        ("design/README.md", "wireframes/v0.1/",
         f"{REPO_URL}/tree/main/docs/design/wireframes/v0.1"),
        ("index.md", "../web/", f"{REPO_URL}/tree/main/web"),
    ],
)  # fmt: skip
def test_links_leaving_the_site_go_to_github(page: str, target: str, expected: str | None) -> None:
    assert outside_url(page, target) == expected


def test_broken_links_outside_the_site_are_errors() -> None:
    with pytest.raises(FileNotFoundError):
        outside_url("quickstart.md", "../no/such/file.md")
    with pytest.raises(ValueError, match="leaves the repository"):
        outside_url("index.md", "../../elsewhere.md")


def test_rewrite_leaves_code_blocks_alone(caplog: pytest.LogCaptureFixture) -> None:
    text = (
        "See [the contracts](../contracts/README.md) and [the API](api.md).\n"
        "```bash\necho '[not a link](../nope.md)'\n```\n"
        "Broken: [gone](../gone.md).\n"
    )
    out = rewrite(text, "quickstart.md")
    assert f"[the contracts]({REPO_URL}/blob/main/contracts/README.md)" in out
    assert "[the API](api.md)" in out
    assert "[not a link](../nope.md)" in out  # inside the fence: untouched, not checked
    assert "[gone](../gone.md)" in out
    [warning] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert "../gone.md" in warning.getMessage()  # a warning fails mkdocs build --strict


def nav_files(nav: list[Any]) -> list[str]:
    found = []
    for item in nav:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                found += nav_files(value) if isinstance(value, list) else [value]
    return found


def test_the_site_lists_existing_pages_and_every_playbook() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    assert config["docs_dir"] == "docs"
    assert config["hooks"] == ["scripts/docs_links.py"]
    pages = nav_files(config["nav"])
    missing = [p for p in pages if not (ROOT / "docs" / p).is_file()]
    assert missing == []
    playbooks = sorted(f"playbooks/{p.name}" for p in (ROOT / "docs" / "playbooks").glob("*.md"))
    assert set(playbooks) <= set(pages), "every playbook is in the menu"
    assert {"index.md", "quickstart.md", "api.md"} <= set(pages)


def test_ci_builds_the_site_strictly() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "mkdocs build --strict" in ci
