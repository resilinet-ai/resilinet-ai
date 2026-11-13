# Audit hash chain

RAI records every state change in `rai.audit_chain`, with one hash chain per tenant (FR-01.2). This page defines the format exactly, so anyone can verify a chain without trusting RAI's own code.

## What a valid chain proves, and what it doesn't (NFR-AUD-2)

- **It proves** that the stored entries have not been edited, removed, reordered or replayed since they were chained.
- **It is tamper-evident, not tamper-proof.** Someone with database superuser rights could rewrite an entry *and every hash after it*. Comparing the current chain head with a head exported earlier (for example, the `chain_head_hash` in an evidence bundle) exposes that.
- **It does not prove** that the recorded facts were true. It only shows they weren't changed after being recorded.

## How entries are chained

A database trigger, `rai.audit_chain_link`, chains each new entry. The application cannot supply or override these fields. For each new entry, the trigger:

1. takes a transaction-scoped advisory lock for the tenant, so only one writer per chain at a time;
2. assigns `seq` from `rai.audit_chain_seq` after taking the lock, so within a tenant `seq` order is chain order;
3. sets `occurred_at` to the database clock;
4. sets `prev_hash` to the tenant's previous `record_hash`, or 64 zeros for the first entry;
5. sets `record_hash = lowercase_hex(SHA-256(UTF-8(prev_hash || canonical)))`.

Entries are insert-only. The app role has no UPDATE or DELETE grant, and a trigger refuses UPDATE, DELETE and TRUNCATE even from the table owner.

## Canonical form

`canonical` is a JSON array with exactly these nine elements, separated by `,` with no spaces:

| # | Element | Encoding |
| --- | --- | --- |
| 1 | `tenant_id` | JSON string of the lowercase UUID |
| 2 | `seq` | JSON integer |
| 3 | `actor_type` | JSON string (`user`, `api_key`, `worker`, `agent` or `system`) |
| 4 | `actor_id` | JSON string |
| 5 | `action` | JSON string, for example `membership.update` |
| 6 | `subject_type` | JSON string |
| 7 | `subject_id` | JSON string |
| 8 | `occurred_at` | JSON string, UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` (microseconds) |
| 9 | `payload` | PostgreSQL's `jsonb` text output, verbatim (`payload::text`) |

**String escaping** follows PostgreSQL `to_json(text)`:
- `"`, `\` and the control characters `\b \f \n \r \t` use short escapes;
- other characters below U+0020 use lowercase `\u00xx`;
- all other characters, non-ASCII included, appear as themselves.

Python's `json.dumps(value, ensure_ascii=False)` produces the same output.

Example:

```
["01890a5d-ac96-774b-bcce-b302099a8057",1,"user","user-1","membership.insert","membership","1","2026-11-16T09:00:01.000000Z",{"new": {"role": "viewer"}}]
```

## Verifying a chain

- **Outside the database:** run `rai audit verify --tenant <uuid>`. This recomputes the chain in Python (`rai.core.audit.verify_chain`) and exits 1 at the first break, naming the `seq` and the reason.
- **Inside the database:** run `SELECT * FROM rai.audit_verify('<uuid>')`.

The test suite checks that the two verifiers agree, including on tampered chains, and that the Python canonical text matches SQL `rai.audit_canonical` byte for byte.
