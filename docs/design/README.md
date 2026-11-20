# Design baseline

The design set has six documents (Exhibits 15–17).

## Status: v0.3 drafts are the baseline

**The current documents are the baseline (decision DESIGN-V03, 2026-11-20).** The design set was first drafted on 2026-11-13 (v0.1, build journal entry 1), also with AI assistance, outside this repository. Those drafts were found on 2026-11-20, after the build had moved past them. The author chose to keep the current documents and to take from v0.1 only what they lacked:

- **Specification:** an acceptance criterion for each requirement, a glossary, the user journeys J1–J5 and success measures G1–G8.
- **Protocol:** hypotheses H1–H6 with decision rules, the S01–S20 scenario table adapted to SandboxBank as built, the analysis plan, the replication criterion and threats to validity.
- **Architecture:** the risk register.
- **Build Plan:** B-41, the capacity levers, the test targets and the gate dates; and new backlog items B-42 to B-46 for features v0.1 described that the build lacks.

The v0.1 drafts are not kept here and do not take precedence.

**v0.2 reconstructed drafts, 2026-11-17.** An AI assistant (Claude Code) reconstructed each document from the repository's own records: the ADRs, the decision log, the threat model, the traceability matrix, the build journal, the code and the tests. Each one keeps the section numbers the code cites. **All documents remain drafts pending review and approval by the author of record (ADR-007).**

**Revised 2026-11-18.** The drafts were checked against `docs/traceability.csv`: every requirement ID they cite is in the matrix, the Specification's requirement tables repeat the matrix's IDs, priorities and wording exactly, and the Build Plan's backlog lists the requirements each item serves, read from the matrix. The PDFs were re-exported; file names carry the export date.

**One v0.1 screen kept, 2026-11-18.** The v0.1 wireframe *01 Executive dashboard (FR-11)*, made on 2026-11-13 in a visual design tool, was found and is kept unchanged in [`wireframes/v0.1/`](wireframes/v0.1/). The Wireframes document now draws ten screens in its design language: a self-contained HTML set, [`wireframes/rai-wireframes.html`](wireframes/rai-wireframes.html), with the four themes switchable. Its PNGs are rendered by `scripts/render_wireframes.py`.

| Document | Source (edit this) | Export |
| --- | --- | --- |
| Product & System Requirements Specification | [`rai-spec.md`](rai-spec.md) | `2026-11-20-rai-spec-v0.3-draft.pdf` |
| Architecture & Design | [`rai-architecture.md`](rai-architecture.md) | `2026-11-20-rai-architecture-v0.3-draft.pdf` |
| Data Model & API Contract | [`rai-data-model-api.md`](rai-data-model-api.md) | `2026-11-20-rai-data-model-api-v0.3-draft.pdf` |
| MVP Wireframes | [`rai-wireframes.md`](rai-wireframes.md) | `2026-11-18-rai-wireframes-v0.2-draft.pdf` (the v0.3 text changes are small; the 1.4 MB PDF is re-exported with the next screen changes) |
| Validation & Benchmark Protocol | [`rai-protocol.md`](rai-protocol.md) | `2026-11-20-rai-protocol-v0.3-draft.pdf` |
| Build Plan | [`rai-build-plan.md`](rai-build-plan.md) | `2026-11-20-rai-build-plan-v0.3-draft.pdf` |

## Updating

The Markdown is the source of truth. After editing, export dated PDFs with a new version, never overwriting an earlier export:

```bash
uv run python scripts/export_design_pdfs.py --date YYYY-MM-DD --version v0.3
```

When the author approves a document, change its **Status** line to *Approved by …, date* and export it with a version without `-draft`.
