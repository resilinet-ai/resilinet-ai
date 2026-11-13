# Decision log

Every design or scope decision, newest first. Owner: Trevor Kauyu.

| Date | ID | Decision | Options considered | Why |
| --- | --- | --- | --- | --- |
| 2026-11-13 | SBX-SCHEMA | SandboxBank schema v1 (accounts, merchants, transactions_raw) as CSV; stdlib-only seeded generator; seed ranges enforced (dev 0-99,999, eval 100,000-199,999) | Parquet output; Faker/NumPy generator | Spec gave no column schema; CSV and stdlib integer draws give byte-identical output on every platform. See sandboxbank/README.md |
| 2026-11-13 | PACE | Gate-driven pace: a sprint closes as soon as its gate passes; dates are latest-by | Fixed calendar sprints | Deliver the full workstream sooner than planned |
| 2026-11-13 | BUILD | Day 1 = 2026-11-16; ~25 h/week baseline; AI may draft, Trevor reviews and owns every change | - | Build plan v0.1, section 9 |
| 2026-11-13 | PROTO | Reviewer roles defined by criteria; key custodian; OSF registration under embargo | Named reviewers; GitHub-only timestamp | Flexibility without protocol deviations |
| 2026-11-13 | D10 | Midnight (dark) default UI theme; Clarity, Evergreen, High contrast selectable | Light default | Owner preference; all themes must meet WCAG 2.2 AA |
| 2026-11-13 | ADR-010 | Modular monolith, three planes | Microservices | See docs/adr/ADR-010.md |
| 2026-11-13 | D1-D9 | Accepted ADR-001 to ADR-009 | See spec section 11 | See docs/adr/ |
