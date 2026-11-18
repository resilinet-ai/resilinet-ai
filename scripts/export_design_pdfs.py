"""Export the design documents in docs/design/*.md as dated PDFs (design baseline).

    uv run python scripts/export_design_pdfs.py --date 2026-11-17 --version v0.2-draft

Renders each Markdown file to HTML (markdown-it-py, already in the lock as a
dependency of rich) and prints it to PDF with a local headless Chrome or Edge
(set RAI_CHROME to its path if it is not found). The Markdown is the source of
truth; the PDFs are exports of it. Images (the wireframe SVGs) resolve relative to
docs/design/, as they do on GitHub.
"""

from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs" / "design"
DOCUMENTS = ("spec", "architecture", "data-model-api", "wireframes", "protocol", "build-plan")
CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
)  # fmt: skip

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm; }
body { font: 10pt/1.45 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; color: #1a1a1a; }
h1 { font-size: 18pt; margin: 0 0 8pt; color: #0b3954; }
h2 { font-size: 13pt; margin: 16pt 0 6pt; color: #0b3954; border-bottom: 1px solid #c9d6df; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9pt; }
th, td { border: 1px solid #c9d6df; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #eef3f7; }
code { font: 8.5pt Consolas, "Courier New", monospace; background: #f3f5f7; padding: 0 2pt; }
pre { background: #f3f5f7; padding: 6pt; font-size: 7.5pt; line-height: 1.25;
      white-space: pre; overflow: hidden; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 7.5pt; }
blockquote { margin: 8pt 0; padding: 6pt 10pt; background: #fff7e0;
             border-left: 4px solid #e0a800; }
tr, li { page-break-inside: avoid; }
img { display: block; max-width: 100%; height: auto; margin: 4pt 0 10pt;
      page-break-inside: avoid; }
.footer { margin-top: 18pt; font-size: 8pt; color: #555; border-top: 1px solid #c9d6df;
          padding-top: 4pt; }
"""


def browser() -> str:
    for candidate in (os.environ.get("RAI_CHROME"), *CANDIDATES):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    raise SystemExit("no Chrome or Edge found; set RAI_CHROME to its path")


def render(source: Path, footer: str) -> str:
    body = MarkdownIt("commonmark").enable("table").render(source.read_text(encoding="utf-8"))
    title = source.read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(title)}"
        f"</title><base href='{DESIGN.as_uri()}/'><style>{CSS}</style></head><body>{body}"
        f"<div class='footer'>{html.escape(footer)}</div></body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, help="Date in the file names, YYYY-MM-DD")
    parser.add_argument("--version", required=True, help="Version in the file names: v0.2-draft")
    args = parser.parse_args(argv)
    chrome = browser()
    with tempfile.TemporaryDirectory() as tmp:
        for name in DOCUMENTS:
            source = DESIGN / f"rai-{name}.md"
            target = DESIGN / f"{args.date}-rai-{name}-{args.version}.pdf"
            page = Path(tmp) / f"{name}.html"
            footer = (f"Exported from docs/design/{source.name} ({args.version}, {args.date}). "
                      "The Markdown in the repository is the source of truth.")  # fmt: skip
            page.write_text(render(source, footer), encoding="utf-8")
            subprocess.run(  # noqa: S603 - a local browser on a file we just wrote
                [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                 f"--print-to-pdf={target}", page.as_uri()],
                check=True, capture_output=True, timeout=120,
            )  # fmt: skip
            print(f"wrote {target.relative_to(ROOT)} ({target.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
