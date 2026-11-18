# ruff: noqa: E501, RUF001
# Screen copy is drawn text: long lines and typographic characters are the content.
"""Draw the MVP wireframes (docs/design/rai-wireframes.md, W1-W8) as SVG files.

    uv run python scripts/draw_wireframes.py

Writes docs/design/wireframes/w1-sign-in.svg ... w8-evidence.svg. The screens are
low-fidelity: layout, content and the rules every screen follows (synthetic-data
banner, metric denominators, status as a word and an icon, disabled controls that
say why). Field names follow the API, so each value shown has a source; the values
themselves are illustrative. Edit the screen functions below and re-run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "design" / "wireframes"

W = 1040  # canvas width
FX, FW = 20, 1000  # frame x and width
NAV_W = 160
CX = FX + NAV_W + 20  # content x
CW = FX + FW - 20 - CX  # content width

STYLE = """
text { font-family: "Segoe UI", system-ui, -apple-system, Arial, sans-serif; fill: #1f2933; }
.t { font-size: 12px; }
.s { font-size: 11px; fill: #52606d; }
.b { font-size: 12px; font-weight: 600; }
.h1 { font-size: 18px; font-weight: 600; }
.h2 { font-size: 14px; font-weight: 600; }
.title { font-size: 15px; font-weight: 700; fill: #0b3954; }
.tag { font-size: 11px; font-weight: 600; fill: #8a4b00; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 11.5px; }
.big { font-size: 24px; font-weight: 600; }
.ban { font-size: 12px; font-weight: 700; fill: #5c3d00; letter-spacing: .4px; }
.navt { font-size: 12px; fill: #323f4b; }
.nava { font-size: 12px; font-weight: 600; fill: #0b3954; }
.btnt { font-size: 12px; font-weight: 600; fill: #ffffff; }
.btn2t { font-size: 12px; font-weight: 600; fill: #0b3954; }
.dist { font-size: 12px; font-weight: 600; fill: #9aa5b1; }
.cot { font-size: 11px; font-weight: 700; fill: #ffffff; }
.note { font-size: 11.5px; fill: #323f4b; }
.frame { fill: #ffffff; stroke: #7b8794; stroke-width: 1.5; }
.panel { fill: #ffffff; stroke: #cbd2d9; }
.fill { fill: #f5f7fa; stroke: #cbd2d9; }
.thead { fill: #e4e7eb; stroke: #cbd2d9; }
.sel { fill: #e6f0f7; stroke: #0b3954; }
.input { fill: #ffffff; stroke: #9aa5b1; }
.banner { fill: #fff1c7; stroke: #c98a00; }
.nav { fill: #f5f7fa; stroke: #cbd2d9; }
.btn { fill: #0b3954; }
.btn2 { fill: #ffffff; stroke: #0b3954; }
.dis { fill: #f5f7fa; stroke: #cbd2d9; stroke-dasharray: 4 3; }
.chip { fill: #ffffff; stroke: #7b8794; }
.chipbad { fill: #ffffff; stroke: #1f2933; stroke-width: 2; }
.co { fill: #c2410c; }
.bar { fill: #9fb3c8; }
.line { stroke: #cbd2d9; }
.arrow { stroke: #52606d; stroke-width: 1.5; fill: none; marker-end: url(#a); }
"""

Cell = str | tuple[str, str]


class Svg:
    def __init__(self, height: int) -> None:
        self.h = height
        self.parts: list[str] = []

    def rect(self, x: float, y: float, w: float, h: float, cls: str, rx: float = 4) -> None:
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" class="{cls}"/>'
        )

    def text(self, x: float, y: float, s: str, cls: str = "t", anchor: str = "start") -> None:
        a = "" if anchor == "start" else f' text-anchor="{anchor}"'
        self.parts.append(f'<text x="{x}" y="{y}" class="{cls}"{a}>{escape(s)}</text>')

    def line(self, x1: float, y1: float, x2: float, y2: float, cls: str = "line") -> None:
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}"/>')

    def render(self, label: str) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{self.h}" '
            f'viewBox="0 0 {W} {self.h}" role="img" aria-label="{escape(label)}">'
            f"<style>{STYLE}</style>"
            '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="#52606d"/>'
            "</marker></defs>"
            f'<rect width="{W}" height="{self.h}" fill="#ffffff"/>' + "".join(self.parts) + "</svg>"
        )

    # Widgets ---------------------------------------------------------------------------

    def button(self, x: float, y: float, w: float, label: str, kind: str = "btn") -> None:
        self.rect(x, y, w, 28, kind)
        cls = {"btn": "btnt", "btn2": "btn2t", "dis": "dist"}[kind]
        self.text(x + w / 2, y + 18.5, label, cls, "middle")

    def chip(
        self, x: float, y: float, label: str, bad: bool = False, w: float | None = None
    ) -> float:
        width = w or 16 + 6.6 * len(label)
        self.rect(x, y, width, 20, "chipbad" if bad else "chip", rx=10)
        self.text(x + width / 2, y + 14, label, "b" if bad else "t", "middle")
        return width

    def field(self, x: float, y: float, w: float, label: str, value: str = "") -> None:
        self.text(x, y, label, "s")
        self.rect(x, y + 6, w, 28, "input", rx=3)
        if value:
            self.text(x + 8, y + 25, value)

    def callout(self, n: int, x: float, y: float) -> None:
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="9" class="co"/>')
        self.text(x, y + 4, str(n), "cot", "middle")

    def table(
        self, x: float, y: float, cols: Sequence[tuple[str, float]], rows: Sequence[Sequence[Cell]],
        row_h: float = 28, select: int | None = None,
    ) -> float:  # fmt: skip
        width = sum(w for _, w in cols)
        self.rect(x, y, width, row_h, "thead", rx=0)
        cx = x
        for head, w in cols:
            self.text(cx + 8, y + 18, head, "b")
            cx += w
        for i, row in enumerate(rows):
            ry = y + row_h * (i + 1)
            self.rect(x, ry, width, row_h, "sel" if i == select else "panel", rx=0)
            cx = x
            for cell, (_, w) in zip(row, cols, strict=True):
                if isinstance(cell, tuple):
                    label, style = cell
                    if style in ("chip", "bad"):
                        self.chip(cx + 6, ry + 4, label, bad=style == "bad")
                    else:
                        self.text(cx + 8, ry + 18, label, style)
                else:
                    self.text(cx + 8, ry + 18, cell)
                cx += w
        return y + row_h * (len(rows) + 1)

    def tile(self, x: float, y: float, w: float, h: float, title: str, value: str,
             lines: Sequence[str]) -> None:  # fmt: skip
        self.rect(x, y, w, h, "panel")
        self.text(x + 12, y + 22, title, "b")
        self.text(x + w - 14, y + 22, "ⓘ", "b", "end")
        self.text(x + 12, y + 56, value, "big")
        for i, line in enumerate(lines):
            self.text(x + 12, y + 80 + i * 16, line, "s")


NAV = ("Overview", "Assets", "Contracts", "Test cases", "Plans", "Scenarios", "Runs",
       "Recovery", "Findings", "Evidence", "Dashboard", "API keys")  # fmt: skip


def chrome(
    svg: Svg, wid: str, name: str, crumbs: str, active: str | None, height: int,
    planned: str | None = None, banner: str = "SYNTHETIC TEST DATA — NOT A PRODUCTION RESULT",
    user: str = "A. User (engineer)",
) -> None:  # fmt: skip
    """Frame, title, banner, top bar and (when active is set) the side navigation."""
    svg.text(FX, 26, f"{wid}  {name}", "title")
    if planned:
        svg.text(FX + FW, 26, planned, "tag", "end")
    svg.rect(FX, 40, FW, height, "frame", rx=6)
    svg.rect(FX + 1, 41, FW - 2, 30, "banner", rx=5)
    svg.text(FX + 16, 61, "⚠  " + banner, "ban")
    svg.line(FX, 112, FX + FW, 112)
    svg.rect(FX + 14, 80, 46, 24, "btn", rx=4)
    svg.text(FX + 37, 97, "RAI", "btnt", "middle")
    svg.text(FX + 74, 97, crumbs)
    if active is not None:
        svg.rect(FX + FW - 404, 80, 132, 24, "input", rx=3)
        svg.text(FX + FW - 396, 96, "Env: SANDBOX-01  ▾", "s")
        svg.rect(FX + FW - 262, 80, 112, 24, "input", rx=3)
        svg.text(FX + FW - 254, 96, "Theme: Midnight ▾", "s")
        svg.text(FX + FW - 16, 97, user + "  ▾", "t", "end")
        svg.rect(FX + 1, 113, NAV_W, height - 74, "nav", rx=0)
        for i, item in enumerate(NAV):
            y = 140 + i * 30
            if item == active:
                svg.rect(FX + 8, y - 16, NAV_W - 16, 26, "sel")
            svg.text(FX + 20, y + 2, item, "nava" if item == active else "navt")


def notes(svg: Svg, y: float, items: Sequence[str]) -> None:
    svg.text(FX, y, "Notes", "h2")
    for i, item in enumerate(items):
        svg.callout(i + 1, FX + 9, y + 22 + i * 22)
        svg.text(FX + 26, y + 26 + i * 22, item, "note")


def height_for(frame_h: int, n_notes: int) -> int:
    return 40 + frame_h + 40 + n_notes * 22 + 20


# Screens ----------------------------------------------------------------------------------


def w1() -> tuple[str, Svg]:
    frame_h, n = 470, 5
    s = Svg(height_for(frame_h, n))
    chrome(s, "W1", "Sign-in and tenant choice", "Sign in", None, frame_h,
           banner="RAI RUNS ON SANDBOX AND STAGING DATA ONLY — NEVER PRODUCTION")  # fmt: skip
    # Sign-in card
    x, y = 110, 150
    s.rect(x, y, 360, 320, "panel", rx=6)
    s.text(x + 24, y + 38, "Sign in to RAI", "h1")
    s.field(x + 24, y + 72, 312, "Email", "a.user@example.org")
    s.field(x + 24, y + 128, 312, "Password", "••••••••••••")
    s.button(x + 24, y + 184, 312, "Sign in")
    s.rect(x + 24, y + 228, 312, 30, "fill", rx=3)
    s.text(x + 34, y + 247, "✕  Email or password is wrong", "b")
    s.callout(1, x + 350, y + 243)
    s.text(x + 24, y + 284, "After repeated failures: “Too many failed sign-ins;", "s")
    s.text(x + 24, y + 300, "try again later” (Retry-After shown as a countdown).", "s")
    s.callout(2, x + 350, y + 292)
    s.parts.append(f'<path d="M{x + 372} {y + 160} L{x + 410} {y + 160}" class="arrow"/>')
    # Tenant chooser
    x2 = 530
    s.rect(x2, y, 400, 320, "panel", rx=6)
    s.text(x2 + 24, y + 38, "Choose a tenant", "h1")
    s.text(x2 + 24, y + 60, "Signed in as A. User · a.user@example.org", "s")
    s.callout(3, x2 + 384, y + 34)
    tenants = (("Credit Union Sandbox", "cu-sandbox", "engineer"),
               ("Research Lab", "research-lab", "reviewer"),
               ("Demo Fintech", "demo-fintech", "viewer"))  # fmt: skip
    for i, (nm, slug, role) in enumerate(tenants):
        ty = y + 80 + i * 54
        s.rect(x2 + 24, ty, 352, 44, "sel" if i == 0 else "fill", rx=4)
        s.text(x2 + 38, ty + 19, nm, "b")
        s.text(x2 + 38, ty + 35, slug, "s")
        s.chip(x2 + 260, ty + 12, role, w=82)
        s.text(x2 + 362, ty + 27, "›", "b")
    s.text(x2 + 24, y + 258, "No tenant listed? Tenants are created by the operator", "s")
    s.text(x2 + 24, y + 274, "(rai admin create-tenant); ask an admin to invite you.", "s")
    s.callout(4, x2 + 384, y + 266)
    s.callout(5, FX + FW - 14, 56)
    notes(s, 40 + frame_h + 30, (
        "The same message for an unknown email, a wrong password and a disabled or invited account (threat S-01).",
        "Failures are throttled per email and per address; the wait comes from the API's Retry-After header.",
        "Tenants and roles come from GET /me (FR-01.1); the role is shown as a word, not a colour.",
        "The API cannot create tenants (threat E-04), so the screen explains how to get one instead of offering a button.",
        "Before a project is chosen the banner states the operating boundary; afterwards it shows the project's classification (FR-11.2).",
    ))  # fmt: skip
    return "w1-sign-in", s


def w2() -> tuple[str, Svg]:
    frame_h, n = 500, 4
    s = Svg(height_for(frame_h, n))
    chrome(s, "W2", "Project overview", "Credit Union Sandbox  ›  Projects  ›  sandboxbank-payments",
           "Overview", frame_h)  # fmt: skip
    y = 150
    s.text(CX, y, "sandboxbank-payments", "h1")
    s.chip(CX + 216, y - 15, "synthetic", w=84)
    s.callout(1, CX + 312, y - 5)
    s.text(CX, y + 22, "Default environment SANDBOX-01 · owner A. User · 3 assets · 4 plans", "s")
    s.button(CX + CW - 130, y - 20, 130, "Start run  ▾")
    s.text(CX, y + 58, "Assets", "h2")
    end = s.table(CX, y + 68, (("Asset", 220), ("Kind", 90), ("Latest contract", 190),
                               ("Last validated", 140), ("Result", 160)), (
        ("transactions_raw", "table", "transactions_raw v1", "12:01 UTC", ("✓ pass", "chip")),
        ("ledger_transactions", "table", "ledger v2", "11:06 UTC", ("✕ fail", "bad")),
        ("fact_transaction", "table", "—  (no contract)", "—", ("— not run", "t")),
    ))  # fmt: skip
    s.callout(2, CX + CW - 12, y + 140)
    s.text(CX, end + 34, "Recent runs", "h2")
    s.text(CX + CW, end + 34, "Showing 3 of 41 · last 7 days", "s", "end")
    s.callout(3, CX + CW - 196, end + 30)
    s.table(CX, end + 44, (("Run", 90), ("Plan", 130), ("Trigger", 200), ("State", 120),
                           ("Verdict", 100), ("Queued", 80), ("Duration", 80)), (
        ("9d70…", "ci-contracts v3", "CI · key rai_7f3a · a1b2c3d", ("✓ passed", "chip"),
         ("pass", "t"), "12:01", "18 s"),
        ("9c1f…", "outage-check v1", "manual · A. User", ("✕ failed", "bad"),
         ("fail", "b"), "11:02", "4 min 10 s"),
        ("98e2…", "ci-contracts v3", "CI · key rai_7f3a · 0f9e8d7", ("⊘ refused", "chip"),
         ("—", "t"), "10:40", "1 s"),
    ))  # fmt: skip
    s.callout(4, CX + 400, end + 58)
    notes(s, 40 + frame_h + 30, (
        "The project's data classification (synthetic or authorized_snapshot) is always visible and drives the banner text.",
        "Results are a word plus an icon, never colour alone (NFR-USE-2). A table with no contract says so instead of showing a blank.",
        "Every list states how many items it shows, of how many, and over which window (NFR-USE-1).",
        "CI runs name the API key prefix and the commit (trigger_ref); a refused run shows its state, not a verdict.",
    ))  # fmt: skip
    return "w2-project-overview", s


def w3() -> tuple[str, Svg]:
    frame_h, n = 640, 4
    s = Svg(height_for(frame_h, n))
    chrome(s, "W3", "Run detail: steps, observations, lineage",
           "… › sandboxbank-payments  ›  Runs  ›  9c1f2a7e", "Runs", frame_h)  # fmt: skip
    y = 148
    s.text(CX, y, "Run 9c1f2a7e · outage-check v1", "h1")
    s.chip(CX + 300, y - 15, "✕ failed · verdict fail", bad=True, w=160)
    # state stepper
    states = ("queued 11:02:01", "preflight", "running 11:02:03", "evaluating", "failed 11:06:11")
    sx = CX
    for i, st in enumerate(states):
        s.rect(sx, y + 14, 140, 24, "fill" if i < 4 else "chipbad", rx=12)
        s.text(sx + 70, y + 30, st, "s" if i < 4 else "b", "middle")
        if i < 4:
            s.parts.append(f'<path d="M{sx + 142} {y + 26} L{sx + 158} {y + 26}" class="arrow"/>')
        sx += 160
    # pins
    s.rect(CX, y + 50, CW, 60, "fill")
    s.text(CX + 12, y + 70, "Pinned at queue time", "b")
    s.text(CX + 12, y + 92, "plan outage-check v1 · sha256 9f3c…e1 · scenario ledger-outage v2 · approval "
           "R. Reviewer 10:58 · SANDBOX-01 · rai 0.1.0", "s")  # fmt: skip
    s.button(CX + CW - 140, y + 54, 128, "View pins JSON", "btn2")
    s.callout(1, CX + CW - 152, y + 72)
    # steps
    s.text(CX, y + 140, "Steps", "h2")
    end = s.table(CX, y + 150, (("Step", 90), ("Plan item", 220), ("State", 90), ("Verdict", 90),
                                ("Attempt", 70), ("Summary", 240)), (
        ("item-1", "scenario ledger-outage v2", ("done", "t"), ("✕ fail", "bad"), "1",
         "RTO 142 s exceeds objective 120 s"),
        ("item-2", "ledger.consistency v1", ("done", "t"), ("✓ pass", "chip"), "1",
         "0 missing · 0 duplicated · 0 mismatched"),
    ))  # fmt: skip
    # observations
    s.text(CX, end + 32, "Observations", "h2")
    tx = CX + 110
    for label, active in (
        ("All 8", True),
        ("Fail 1", False),
        ("Error 0", False),
        ("Skipped 0", False),
    ):
        s.rect(tx, end + 17, 74, 22, "sel" if active else "fill", rx=11)
        s.text(tx + 37, end + 32, label, "b" if active else "s", "middle")
        tx += 80
    end2 = s.table(CX, end + 46, (("Step", 110), ("Kind", 70), ("Rule", 160), ("Outcome", 100),
                                  ("Observed", 200), ("Expected", 160)), (
        ("item-1.fault-1", "metric", "fault.onset", ("✓ pass", "chip"), "onset 11:02:14Z", "—"),
        ("item-1", "metric", "recovery.rto", ("✕ fail", "bad"), "142.0 s", "≤ 120 s"),
        ("item-1", "metric", "recovery.rpo", ("✓ pass", "chip"), "0 s · 0 rows lost", "≤ 0 s"),
    ))  # fmt: skip
    s.callout(2, CX + CW - 12, end + 30)
    # lineage
    s.text(CX, end2 + 34, "Lineage", "h2")
    ly = end2 + 46
    s.rect(CX, ly, 230, 48, "panel")
    s.text(CX + 12, ly + 20, "ledger_transactions", "b")
    s.text(CX + 12, ly + 38, "sha256 4be1…09 · 190,002 rows", "s")
    s.parts.append(f'<path d="M{CX + 232} {ly + 24} L{CX + 290} {ly + 24}" class="arrow"/>')
    s.rect(CX + 292, ly, 200, 48, "fill")
    s.text(CX + 304, ly + 20, "item-2 consistency check", "b")
    s.text(CX + 304, ly + 38, "transform ledger.consistency v1", "s")
    s.parts.append(f'<path d="M{CX + 494} {ly + 24} L{CX + 552} {ly + 24}" class="arrow"/>')
    s.rect(CX + 554, ly, 230, 48, "panel")
    s.text(CX + 566, ly + 20, "fact_transaction", "b")
    s.text(CX + 566, ly + 38, "sha256 77a0…3c · 190,002 rows", "s")
    s.callout(3, CX + 794, ly + 24)
    s.callout(4, CX + 484, y - 5)
    notes(s, 40 + frame_h + 30, (
        "Everything the worker ran from is pinned with its digest, so the run can be replayed (NFR-REP-1, FR-06.2).",
        "Each observation shows observed and expected side by side; outcome is a word plus an icon (NFR-USE-2).",
        "Lineage shows the SHA-256 and row count of the data the run actually read (FR-04.1).",
        "A finished run never changes; the state machine is enforced in the database, so the page has no edit controls.",
    ))  # fmt: skip
    return "w3-run-detail", s


def w4() -> tuple[str, Svg]:
    frame_h, n = 560, 4
    s = Svg(height_for(frame_h, n))
    chrome(s, "W4", "Chaos scenario: definition and review",
           "… › sandboxbank-payments  ›  Scenarios  ›  ledger-outage v2", "Scenarios", frame_h, user="A. User (admin)")  # fmt: skip
    y = 148
    s.text(CX, y, "ledger-outage · version 2", "h1")
    s.chip(CX + 236, y - 15, "◷ pending review", w=130)
    s.text(
        CX,
        y + 22,
        "Author A. User (you) · created 2026-11-18 10:41 UTC · environment SANDBOX-01",
        "s",
    )
    left_w = 470
    s.text(CX, y + 58, "Blast radius", "h2")
    bx: float = CX
    for t in ("ledger-db-proxy", "payments-api"):
        bx += s.chip(bx, y + 68, "◎ " + t) + 8
    s.text(bx + 4, y + 82, "both on the SANDBOX-01 allow-list", "s")
    s.callout(1, CX + left_w - 10, y + 54)
    s.text(CX, y + 122, "Faults, in order", "h2")
    end = s.table(CX, y + 132, (("#", 40), ("Type", 150), ("Target", 160), ("Duration", 120)), (
        ("1", "connection_drop", "ledger-db-proxy", "60 s"),
        ("2", "container_stop", "payments-api", "30 s"),
    ))  # fmt: skip
    s.text(CX, end + 32, "Recovery plan", "h2")
    rows = (("Probes", "payments-health (2xx)"), ("Ledger head", "payments-ledger"),
            ("Objectives", "RTO ≤ 120 s · RPO ≤ 0 s"), ("Window", "300 s after the last revert"),
            ("Stable checks", "3 consecutive healthy rounds"))  # fmt: skip
    for i, (k, v) in enumerate(rows):
        s.text(CX, end + 56 + i * 22, k, "s")
        s.text(CX + 120, end + 56 + i * 22, v)
    # review panel
    rx, rw = CX + left_w + 20, CW - left_w - 20
    s.rect(rx, y + 40, rw, 380, "fill", rx=6)
    s.text(rx + 16, y + 66, "Review", "h2")
    s.text(rx + 16, y + 88, "The latest decision counts; each version is reviewed.", "s")
    s.callout(2, rx + rw - 16, y + 62)
    s.rect(rx + 16, y + 102, rw - 32, 66, "panel")
    s.text(rx + 28, y + 124, "v1 · 2026-11-17 16:20 · R. Reviewer", "s")
    s.chip(rx + 28, y + 134, "✓ approve", w=86)
    s.text(rx + 124, y + 148, "“radius limited to P1”", "t")
    s.text(rx + 16, y + 194, "No decision yet on version 2.", "b")
    s.field(rx + 16, y + 214, rw - 32, "Comment (optional)")
    s.button(rx + 16, y + 268, 110, "✓ Approve", "dis")
    s.button(rx + 136, y + 268, 110, "✕ Reject", "dis")
    s.rect(rx + 16, y + 308, rw - 32, 48, "panel")
    s.text(rx + 28, y + 328, "You wrote this scenario, so another reviewer", "t")
    s.text(rx + 28, y + 346, "must decide. Approval is refused for its author.", "t")
    s.callout(3, rx + rw - 16, y + 282)
    s.callout(4, CX + 455, y + 146)
    notes(s, 40 + frame_h + 30, (
        "Every target must be on the environment's allow-list with the right kind; the run is refused otherwise (FR-07.2).",
        "Approvals are append-only; the status shown is the latest decision for this version (NFR-AUD-1).",
        "Controls a role cannot use are disabled with the reason; the database also refuses self-approval (FR-07.2, ADR-007).",
        "Durations are checked against the environment's ceiling (default 15 min, at most 60) and again by the chaos agent.",
    ))  # fmt: skip
    return "w4-scenario-review", s


def w5() -> tuple[str, Svg]:
    frame_h, n = 470, 4
    s = Svg(height_for(frame_h, n))
    chrome(
        s, "W5", "Recovery results", "… › sandboxbank-payments  ›  Recovery", "Recovery", frame_h
    )
    y = 148
    s.text(CX, y, "Recovery · ledger-outage", "h1")
    s.rect(CX + CW - 300, y - 18, 300, 26, "input", rx=3)
    s.text(CX + CW - 290, y, "Window: 2026-10-19 – 2026-11-18 (UTC)  ▾", "s")
    s.callout(1, CX + CW - 312, y - 5)
    tw = (CW - 40) / 3
    s.tile(CX, y + 22, tw, 118, "RTO objective met", "4 of 5 runs",
           ("Onset → validated restoration ≤ 120 s", "n = 5 runs in the window"))  # fmt: skip
    s.tile(CX + tw + 20, y + 22, tw, 118, "RPO objective met", "5 of 5 runs",
           ("Lost acknowledged writes ≤ 0 s", "n = 5 runs in the window"))  # fmt: skip
    s.tile(CX + 2 * (tw + 20), y + 22, tw, 118, "Consistency passed", "5 of 5 runs",
           ("Missing, duplicated, mismatched keys", "n = 5 runs in the window"))  # fmt: skip
    s.callout(2, CX + tw - 44, y + 40)
    s.text(CX, y + 176, "Runs", "h2")
    s.table(CX, y + 186, (("Run", 80), ("Finished", 110), ("RTO", 70), ("Objective", 120),
                          ("RPO ledger", 100), ("RPO seeding log", 130), ("Agree", 80), ("Consistent", 110)), (
        ("9c1f…", "11-18 11:06", "142 s", ("✕ missed 120 s", "bad"), "0 s", "0 s", "✓ yes", "✓ yes"),
        ("8a33…", "11-17 15:12", "88 s", ("✓ met", "chip"), "0 s", "0 s", "✓ yes", "✓ yes"),
        ("7f02…", "11-16 09:47", "95 s", ("✓ met", "chip"), "0 s", "0 s", "✓ yes", "✓ yes"),
    ))  # fmt: skip
    s.callout(3, CX + 520, y + 176)
    s.text(CX, y + 330, "Showing 3 of 5 runs.  [ Show all ]", "s")
    s.callout(4, CX + 690, y + 176)
    notes(s, 40 + frame_h + 30, (
        "Every number is bound to the window shown; changing it recomputes all three tiles and the table (NFR-USE-1).",
        "Each tile shows its definition (ⓘ opens the full one) and its denominator (FR-08.1–08.3).",
        "RPO ground truth comes from the seeding log of acknowledged writes, kept outside the system under test (B-30).",
        "If the ledger's RPO and the seeding log disagree, the check fails and the row says “✕ no”.",
    ))  # fmt: skip
    return "w5-recovery", s


def w6() -> tuple[str, Svg]:
    frame_h, n = 470, 4
    s = Svg(height_for(frame_h, n))
    chrome(
        s, "W6", "Executive dashboard", "Credit Union Sandbox  ›  Dashboard", "Dashboard", frame_h
    )
    y = 148
    s.text(CX, y, "Assurance summary", "h1")
    s.rect(CX + CW - 300, y - 18, 300, 26, "input", rx=3)
    s.text(CX + CW - 290, y, "Period: 2026-11-01 – 2026-11-30 (UTC)  ▾", "s")
    tw = (CW - 60) / 4
    tiles = (
        ("Batches passing", "412 of 430", ("95.8% · n = 430 batches", "3 feeds · source: runs")),
        ("Recovery objectives met", "9 of 11", ("n = 11 scenario runs", "source: recovery.*")),
        ("Open critical findings", "2", ("1 waived until 2026-12-15", "n = 37 findings")),
        ("Release gate", "3 blocked", ("of 28 PRs checked", "0 overridden")),
    )  # fmt: skip
    for i, (t, v, lines) in enumerate(tiles):
        s.tile(CX + i * (tw + 20), y + 22, tw, 118, t, v, lines)
    s.callout(1, CX + tw - 44, y + 40)
    s.callout(4, CX + 186, y - 5)
    s.text(CX, y + 176, "Batches passing, by week", "h2")
    s.text(CX + CW, y + 176, "numerator / denominator under each bar", "s", "end")
    weeks = (("W44", 96, 100), ("W45", 101, 104), ("W46", 108, 112), ("W47", 107, 114))
    base = y + 320
    for i, (wk, num, den) in enumerate(weeks):
        bx = CX + 40 + i * 180
        h = 120 * num / den
        s.rect(bx, base - h, 90, h, "bar", rx=2)
        s.text(bx + 45, base - h - 6, f"{num / den:.1%}", "b", "middle")
        s.text(bx + 45, base + 16, f"{wk} · {num} / {den}", "s", "middle")
    s.line(CX + 20, base, CX + CW - 20, base)
    s.callout(2, CX + 30 + 4 * 180, base - 60)
    s.callout(3, CX + CW - 44, y + 40)
    notes(s, 40 + frame_h + 30, (
        "Each tile: ⓘ definition, numerator and denominator, window and data source; a value is never shown alone (FR-11.1, NFR-USE-1).",
        "Bars carry their numbers as text, so the chart is readable without colour or hover (NFR-USE-2).",
        "Findings and gate tiles depend on the findings and waiver APIs (B-33) and show “not available yet” until then.",
        "All values on this screen are illustrative.",
    ))  # fmt: skip
    return "w6-dashboard", s


def w7() -> tuple[str, Svg]:
    frame_h, n = 540, 4
    s = Svg(height_for(frame_h, n))
    chrome(s, "W7", "Findings and waivers", "… › sandboxbank-payments  ›  Findings", "Findings",
           frame_h, planned="PLANNED API · B-33")  # fmt: skip
    y = 148
    s.text(CX, y, "Findings", "h1")
    s.text(CX + 90, y, "Showing 4 of 5 open · 37 in all", "s")
    lw = 400
    s.table(CX, y + 16, (("Severity", 90), ("Finding", 200), ("Gate", 110)), (
        (("✕ critical", "bad"), "Duplicate txn_id across…", ("⛔ blocks", "b")),
        (("✕ critical", "bad"), "RTO objective missed", ("◷ waived", "t")),
        (("▲ high", "chip"), "Drift: amount type", ("— no", "t")),
        (("● medium", "chip"), "Late rows > 1%", ("— no", "t")),
    ), select=0)  # fmt: skip
    s.callout(1, CX + lw - 12, y + 30)
    dx, dw = CX + lw + 20, CW - lw - 20
    s.rect(dx, y - 18, dw, 420, "fill", rx=6)
    s.text(dx + 16, y + 6, "Duplicate txn_id across batches", "h2")
    s.chip(dx + 16, y + 16, "✕ critical", bad=True, w=86)
    s.chip(dx + 110, y + 16, "⛔ blocks the release gate", w=176)
    rows = (("Source", "run 9d70… · observation duplicates.cross_batch"),
            ("Owner", "A. Engineer"), ("Fix", "PR #41 (linked)"),
            ("Retest", "✕ not run yet: a passing retest closes it"))  # fmt: skip
    for i, (k, v) in enumerate(rows):
        s.text(dx + 16, y + 64 + i * 22, k, "s")
        s.text(dx + 90, y + 64 + i * 22, v)
    s.callout(2, dx + dw - 16, y + 64)
    s.line(dx + 16, y + 150, dx + dw - 16, y + 150)
    s.text(dx + 16, y + 174, "Request a waiver", "h2")
    s.field(dx + 16, y + 192, dw - 32, "Reason (required)")
    s.field(dx + 16, y + 248, 170, "Expires on (required)", "2026-12-15")
    s.field(dx + 200, y + 248, dw - 216, "Approver (not you)", "R. Reviewer  ▾")
    s.button(dx + 16, y + 304, 150, "Request waiver")
    s.text(dx + 16, y + 356, "Until a named reviewer approves it, the gate stays blocked.", "s")
    s.text(dx + 16, y + 372, "When the waiver expires, the finding blocks again.", "s")
    s.callout(3, dx + dw - 16, y + 170)
    s.callout(4, dx + dw - 16, y + 262)
    notes(s, 40 + frame_h + 30, (
        "Severity and gate effect are words and icons; the selected row is outlined, not only shaded (NFR-USE-2).",
        "A finding has a severity, an owner, a fix and a retest; it closes only on a passing retest (FR-10.1).",
        "A critical finding blocks the gate unless waived with an expiry date (FR-12.2).",
        "Approval is by a named reviewer, never the requester (FR-12.1). Screen waits on the findings API (B-33).",
    ))  # fmt: skip
    return "w7-findings", s


def w8() -> tuple[str, Svg]:
    frame_h, n = 500, 4
    s = Svg(height_for(frame_h, n))
    chrome(s, "W8", "Evidence export", "… › sandboxbank-payments  ›  Evidence", "Evidence",
           frame_h, planned="PLANNED API · B-34, B-35")  # fmt: skip
    y = 148
    s.text(CX, y, "Export evidence", "h1")
    lw = 360
    s.field(CX, y + 30, lw, "Scope", "Run 9c1f2a7e · outage-check v1  ▾")
    s.text(CX, y + 100, "Contents (always included)", "s")
    items = ("Run pins and digests", "Steps and observations", "Lineage edges",
             "Scenario approvals", "Audit-chain head", "SHA-256 manifest")  # fmt: skip
    for i, it in enumerate(items):
        iy = y + 112 + i * 24
        s.rect(CX, iy, 14, 14, "chip", rx=2)
        s.text(CX + 3, iy + 12, "✓", "b")
        s.text(CX + 24, iy + 12, it)
    s.callout(1, CX + lw - 12, y + 96)
    s.button(CX, y + 270, 170, "Export bundle")
    s.text(CX, y + 318, "Synthetic batches are not included;", "s")
    s.text(CX, y + 334, "the manifest records their digests.", "s")
    rx, rw = CX + lw + 30, CW - lw - 30
    s.rect(rx, y - 18, rw, 380, "fill", rx=6)
    s.text(rx + 16, y + 6, "Last export", "h2")
    rows = (("File", "rai-evidence-9c1f2a7e.zip · 412 KB"),
            ("Manifest", "sha256 3e7a…d0 · 37 files"),
            ("Chain head", "seq 1,204 · 8d2c…7b"),
            ("Signature", "Ed25519 · key rai-evidence-1"))  # fmt: skip
    for i, (k, v) in enumerate(rows):
        s.text(rx + 16, y + 34 + i * 22, k, "s")
        s.text(rx + 100, y + 34 + i * 22, v, "mono" if k != "File" else "t")
    s.callout(2, rx + rw - 16, y + 34)
    s.text(rx + 16, y + 140, "Verify anywhere", "b")
    s.rect(rx + 16, y + 150, rw - 32, 76, "panel")
    s.text(rx + 28, y + 172, "$ rai verify rai-evidence-9c1f2a7e.zip", "mono")
    s.text(rx + 28, y + 192, "✓ 37 files match the manifest", "mono")
    s.text(rx + 28, y + 210, "✓ signature valid · ✓ chain head matches", "mono")
    s.callout(3, rx + rw - 16, y + 140)
    s.rect(rx + 16, y + 244, rw - 32, 64, "panel")
    s.text(rx + 28, y + 266, "Tamper-evident, not tamper-proof: a change to the", "t")
    s.text(rx + 28, y + 284, "bundle or the audit chain is detected, not prevented.", "t")
    s.callout(4, rx + rw - 16, y + 256)
    notes(s, 40 + frame_h + 30, (
        "The contents are fixed, so two exports of the same run are byte-identical (FR-13.1).",
        "The manifest lists every file's SHA-256 and the audit-chain head at export time (FR-13.1).",
        "The signature and rai verify work offline, without the API (FR-13.2).",
        "The page states the guarantee honestly (NFR-AUD-2). Screen waits on the evidence APIs (B-34, B-35).",
    ))  # fmt: skip
    return "w8-evidence", s


SCREENS: tuple[Callable[[], tuple[str, Svg]], ...] = (w1, w2, w3, w4, w5, w6, w7, w8)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for screen in SCREENS:
        name, svg = screen()
        label = (screen.__doc__ or name).strip()
        path = OUT / f"{name}.svg"
        page = svg.render(f"RAI wireframe {label}") + "\n"
        path.write_text(page, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
