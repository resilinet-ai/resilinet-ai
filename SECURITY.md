# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Use GitHub's private vulnerability reporting (Security tab, "Report a vulnerability") on this repository.

Include what you found, how to reproduce it and the affected version. You will get an acknowledgement within 5 working days.

## Scope

Of particular interest:

- Any way to make the chaos agent act on a target that is not on an environment allow-list, or on a PRODUCTION-tagged environment.
- Any cross-tenant data access.
- Any way to alter results, approvals or audit entries without breaking hash-chain verification.
- Secrets appearing in logs, the database or evidence bundles.

## Supported versions

Pre-alpha: only the latest `main` is supported until v1.0.
