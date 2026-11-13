# Contributing to Resilinet-AI

Thank you for your interest. RAI is led and maintained by Trevor Kauyu, who owns the requirements, methodology and evaluation design (see `docs/adr/ADR-007.md`).

## Before you start

- Open or pick an issue. Every feature issue names the requirement it implements (for example `FR-03.3`), listed in `docs/traceability.csv`.
- Read the safety boundaries in `README.md`. Changes that weaken them will not be accepted.

## Making a change

1. Branch from `main`: `feat/FR-03.3-duplicate-keys`.
2. Run `make check` locally (lint, types, tests).
3. Title the pull request in conventional-commit style with the requirement ID: `feat(FR-03.3): detect duplicate keys across batches`. `docs:`, `chore:`, `ci:`, `build:`, `test:`, `style:` and `refactor:` changes may omit the ID.
4. Fill in the pull-request template, including whether AI tools helped write the change.

## Definition of done

- Linked to a requirement ID; acceptance criterion demonstrated by an automated test.
- Changed-module coverage of at least 85%; mypy strict and ruff pass.
- Docs updated where behaviour changed.
- No secrets, real data or personal information.

## Developer Certificate of Origin (DCO)

All commits must be signed off, certifying the [Developer Certificate of Origin 1.1](https://developercertificate.org/):

```bash
git commit -s -m "feat(FR-03.3): detect duplicate keys across batches"
```

Commits to `main` must also be cryptographically signed (SSH or GPG).
