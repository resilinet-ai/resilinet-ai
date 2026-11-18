"""Render the MVP wireframes (docs/design/wireframes/rai-wireframes.html) to PNG.

    uv run python scripts/render_wireframes.py

Writes docs/design/wireframes/png/<screen>.png in the default theme (Midnight) and
dashboard-<theme>.png for the other three themes, using a local headless Chrome or
Edge (RAI_CHROME overrides). The HTML is the source; the PNGs are what the design
document and its PDF show.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def browser() -> str:
    """The Chrome or Edge that export_design_pdfs.py finds (RAI_CHROME overrides)."""
    path = Path(__file__).resolve().parent / "export_design_pdfs.py"
    spec = importlib.util.spec_from_file_location("export_design_pdfs", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.browser())


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "design" / "wireframes" / "rai-wireframes.html"
OUT = SOURCE.parent / "png"
WIDTH = 1440
# Page height per screen at 1440 px wide; raise one if its screenshot is cut off.
SCREENS = {
    "signin": 760,
    "dashboard": 1860,
    "runs": 1380,
    "scenario": 1000,
    "recovery": 900,
    "contracts": 1080,
    "models": 820,
    "approvals": 940,
    "evidence": 860,
    "environments": 900,
}
THEMES = ("clarity", "evergreen", "contrast")


def shot(chrome: str, screen: str, theme: str, height: int, target: Path) -> None:
    url = f"{SOURCE.as_uri()}?screen={screen}&theme={theme}"
    subprocess.run(  # noqa: S603 - a local browser on a file in the repository
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", "--virtual-time-budget=2000",
         f"--window-size={WIDTH},{height}", f"--screenshot={target}", url],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    print(f"wrote {target.relative_to(ROOT)}")


def main() -> int:
    chrome = browser()
    OUT.mkdir(parents=True, exist_ok=True)
    for screen, height in SCREENS.items():
        shot(chrome, screen, "midnight", height, OUT / f"{screen}.png")
    for theme in THEMES:
        shot(chrome, "dashboard", theme, SCREENS["dashboard"], OUT / f"dashboard-{theme}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
