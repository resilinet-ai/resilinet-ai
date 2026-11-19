"""Colour contrast of every UI theme (NFR-USE-2: WCAG 2.2 AA; decision D10).

axe-core runs on the rendered screens in Vitest (``web/src/a11y.test.tsx``), but jsdom
has no computed colours, so contrast is checked here, from the theme tokens in
``web/src/styles.css``, for every pair the UI actually draws:

* text (1.4.3): at least 4.5:1;
* user-interface components and focus indicators (1.4.11): at least 3:1.

Decorative lines (card borders, table rules, the banner outline) carry no meaning on
their own, so they are not held to 1.4.11; every state they mark also has a word.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "web" / "src" / "styles.css"
THEME = re.compile(r'\[data-theme="(\w+)"\]\s*\{([^}]*)\}')
TOKEN = re.compile(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})")

# (foreground, background): where the UI puts one on the other.
TEXT_PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "sunken"), ("text", "hover"),
    ("text", "accent-soft"),  # selected table row
    ("muted", "bg"), ("muted", "surface"), ("muted", "sunken"),
    ("accent", "surface"), ("accent", "bg"),  # links and the "Definition" toggle
    ("accent-ink", "accent-soft"),  # current navigation item
    ("on-accent", "accent"),  # primary button
    ("pass", "pass-bg"), ("fail", "fail-bg"), ("warn", "warn-bg"), ("neutral", "neutral-bg"),
    ("fail", "surface"),  # a bad headline number
    ("banner", "banner-bg"),  # the synthetic-data banner
    ("target", "surface"),  # a chart's target label
]  # fmt: skip
UI_PAIRS = [
    ("control-border", "surface"), ("control-border", "bg"), ("control-border", "sunken"),
    ("focus", "bg"), ("focus", "surface"),  # the 3 px focus outline
    ("accent", "surface"),  # current navigation marker
    ("warn", "neutral-bg"),  # the failing-checks meter
]  # fmt: skip


def themes() -> dict[str, dict[str, str]]:
    found = {
        name: dict(TOKEN.findall(body))
        for name, body in THEME.findall(CSS.read_text(encoding="utf-8"))
        if "--bg:" in body
    }
    assert set(found) == {"clarity", "midnight", "evergreen", "contrast"}
    return found


def luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_formula_matches_wcag() -> None:
    assert contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)
    assert contrast("#123456", "#123456") == pytest.approx(1.0)


@pytest.mark.parametrize("theme", ["clarity", "midnight", "evergreen", "contrast"])
def test_text_and_controls_meet_aa(theme: str) -> None:
    tokens = themes()[theme]
    low_text = [
        f"{fg} on {bg}: {contrast(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in TEXT_PAIRS
        if contrast(tokens[fg], tokens[bg]) < 4.5
    ]
    low_ui = [
        f"{fg} against {bg}: {contrast(tokens[fg], tokens[bg]):.2f}"
        for fg, bg in UI_PAIRS
        if contrast(tokens[fg], tokens[bg]) < 3.0
    ]
    assert low_text == [], "text below 4.5:1 (WCAG 1.4.3)"
    assert low_ui == [], "components below 3:1 (WCAG 1.4.11)"


def test_every_theme_defines_every_token_the_checks_use() -> None:
    needed = {name for pair in TEXT_PAIRS + UI_PAIRS for name in pair}
    for theme, tokens in themes().items():
        assert needed <= set(tokens), f"{theme} lacks {sorted(needed - set(tokens))}"
