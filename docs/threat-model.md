# Threat model (STRIDE) - stub

Status: to be completed in backlog item B-06 (Sprint 1). Requirement: NFR-SEC-4.

Scope: the three planes in Architecture section 1 (control, test-data, evidence) and their trust boundaries.

| Asset | Threat (STRIDE) | Mitigation | Requirement | Status |
| --- | --- | --- | --- | --- |
| Chaos agent Docker access | Elevation of privilege | Socket proxy with allow-listed verbs; agent isolated on bridge_net | NFR-SAF-1 | planned |
| Tenant data | Information disclosure | API checks plus PostgreSQL RLS | NFR-SEC-1 | planned |
| Audit chain | Tampering | Insert-only table, hash chain, daily anchor | FR-01.2 | planned |
