"""MkDocs hook: links that leave ``docs/`` point at the repository on GitHub (FR-14.1).

The guides link to code, contracts and package READMEs outside ``docs/`` (for example
``../sandboxbank/README.md``), and to folders (``wireframes/v0.1/``). Those are not
pages of the site, so this hook rewrites each such link to GitHub, at ``main``,
keeping its ``#anchor``.
A link to a file that does not exist is a warning, and ``mkdocs build --strict``
turns warnings into a failed build: broken links stay caught, inside the site by
MkDocs and outside it here. Links inside fenced code blocks are left alone.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import Any

REPO_URL = "https://github.com/resilinet-ai/resilinet-ai"
ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LINK = re.compile(r"(\]\()([^)\s]+)(\))")
FENCE = re.compile(r"^\s*(```|~~~)")
log = logging.getLogger("mkdocs.hooks.docs_links")


def outside_url(page_src: str, target: str) -> str | None:
    """The GitHub URL for ``target`` (as written in docs/``page_src``) if it leaves
    ``docs/``; None if it stays inside the site or is not a relative file link."""
    if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^/", target, re.IGNORECASE):
        return None  # a URL, an anchor on the page, or a site-absolute path
    path, _, anchor = target.partition("#")
    joined = posixpath.normpath(posixpath.join("docs", posixpath.dirname(page_src), path))
    inside = joined == "docs" or joined.startswith("docs/")
    if inside and not (path.endswith("/") and (ROOT / joined).is_dir()):
        return None  # a page or file of the site; MkDocs checks it
    # A folder is not a page: inside the site too, show it on GitHub.
    if joined.startswith("../"):
        raise ValueError(f"{target!r} leaves the repository")
    resolved = ROOT / joined
    if not resolved.exists():
        raise FileNotFoundError(joined)
    kind = "tree" if resolved.is_dir() else "blob"
    return f"{REPO_URL}/{kind}/main/{joined}" + (f"#{anchor}" if anchor else "")


def rewrite(markdown: str, page_src: str) -> str:
    out, fenced = [], False
    for line in markdown.splitlines(keepends=True):
        if FENCE.match(line):
            fenced = not fenced
        if not fenced:
            line = LINK.sub(lambda m: _link(m, page_src), line)
        out.append(line)
    return "".join(out)


def _link(match: re.Match[str], page_src: str) -> str:
    target = match[2]
    try:
        url = outside_url(page_src, target)
    except (FileNotFoundError, ValueError) as error:
        log.warning("%s: link %r points at a missing file (%s)", page_src, target, error)
        return match[0]
    return match[0] if url is None else f"{match[1]}{url}{match[3]}"


def on_page_markdown(markdown: str, page: Any, **_: Any) -> str:
    return rewrite(markdown, page.file.src_uri)
