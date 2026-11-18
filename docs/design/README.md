# Design baseline

The design set has six documents (Exhibits 15–17).

## Status: v0.1 missing; v0.2 reconstructed drafts pending review

**The v0.1 originals (2026-11-13) are not in this repository.** The build journal (entry 1) records that the design set was written on 2026-11-13, and the code cites its sections throughout. But the PDFs were never exported here, and they were not found on the development machine either. If they are found, export them into this folder under their original names and they take precedence over everything below:

| Document | Version | Original file (missing) |
| --- | --- | --- |
| Product & System Requirements Specification | v0.1 | `2026-11-13-rai-spec-v0.1.pdf` |
| Architecture & Design | v0.1 | `2026-11-13-rai-architecture-v0.1.pdf` |
| Data Model & API Contract | v0.1 | `2026-11-13-rai-data-model-api-v0.1.pdf` |
| MVP Wireframes | v0.1 | `2026-11-13-rai-wireframes-v0.1.pdf` |
| Validation & Benchmark Protocol | v0.1 | `2026-11-13-rai-protocol-v0.1.pdf` |
| Build Plan | v0.1 | `2026-11-13-rai-build-plan-v0.1.pdf` |

**v0.2 reconstructed drafts, 2026-11-17.** An AI assistant (Claude Code) reconstructed each document from the repository's own records: the ADRs, the decision log, the threat model, the traceability matrix, the build journal, the code and the tests. Each one says so at the top, keeps the section numbers the code cites, and lists what could not be recovered rather than inventing it. **They are drafts pending review and approval by the author of record (ADR-007)**, and they are not the v0.1 originals.

| Document | Source (edit this) | Export |
| --- | --- | --- |
| Product & System Requirements Specification | [`rai-spec.md`](rai-spec.md) | `2026-11-17-rai-spec-v0.2-draft.pdf` |
| Architecture & Design | [`rai-architecture.md`](rai-architecture.md) | `2026-11-17-rai-architecture-v0.2-draft.pdf` |
| Data Model & API Contract | [`rai-data-model-api.md`](rai-data-model-api.md) | `2026-11-17-rai-data-model-api-v0.2-draft.pdf` |
| MVP Wireframes | [`rai-wireframes.md`](rai-wireframes.md) | `2026-11-17-rai-wireframes-v0.2-draft.pdf` |
| Validation & Benchmark Protocol | [`rai-protocol.md`](rai-protocol.md) | `2026-11-17-rai-protocol-v0.2-draft.pdf` |
| Build Plan | [`rai-build-plan.md`](rai-build-plan.md) | `2026-11-17-rai-build-plan-v0.2-draft.pdf` |

## Updating

The Markdown is the source of truth. After editing, export dated PDFs with a new version, never overwriting an earlier export:

```bash
uv run python scripts/export_design_pdfs.py --date YYYY-MM-DD --version v0.3
```

When the author approves a document, change its **Status** line to *Approved by …, date* and export it with a version without `-draft`.
