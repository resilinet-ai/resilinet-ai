# Data contracts

A contract declares what one data asset must look like (FR-03.1). `rai` parses it into a flat list of rules that the validation engine runs on every batch.

```
python -m uv run rai contract check contracts/sandboxbank/*.yaml   # validate; list the rule count and sha256
python -m uv run rai contract schema                                 # JSON Schema of the format
```

Point your editor at [`contract.schema.json`](contract.schema.json) for autocompletion. For example, in VS Code with the YAML extension, add `# yaml-language-server: $schema=../contract.schema.json` at the top of a contract file.

## Format

```yaml
contract: transactions_raw          # asset name: lowercase, digits, underscores
version: 1                          # a new version for every change; published versions are immutable
freshness: { column: created_at, max_lag: 1h }   # 30s, 15m, 1h or 2d
primary_key: [txn_id]               # required and unique within the batch
unique_across_batches: [txn_id]     # also unique against earlier batches (FR-03.3)
columns:
  amount:
    type: decimal(14,2)             # string, integer, decimal(p,s), float, boolean, date, timestamp, uuid
    required: true
    min: 0.01                       # min/max: numeric, date or timestamp columns
    max: 25000
    max_lookup: { ref: accounts.txn_limit, key: account_id }   # per-row bound from a reference table
    severity: critical              # info, warn or critical; overrides severity_default (FR-03.4)
    threshold: "0.1%"               # failing rows tolerated: a count (5) or a percentage ("0.1%")
  account_id: { type: string, required: true, pattern: "^ACC-[0-9]{7}$", references: accounts.account_id }
  currency:   { type: string, required: true, allowed: [USD] }
schema_change_policy:               # how schema drift is classified (FR-03.2)
  added_column: non_breaking
  removed_or_renamed_column: breaking
  type_change: breaking
severity_default: critical
threshold_default: 0                # by default any failing row fails the rule
```

**Watch the commas.** In the inline `{ ... }` style, YAML splits values at commas. Write `type: "decimal(14,2)"` in quotes, or put one key per line as above. `rai contract check` explains the error if you forget.

The parser rejects:
- unknown keys and duplicate keys;
- options that don't fit the column type, such as `pattern` on a number, or `min` greater than `max`;
- invalid regular expressions;
- references to undeclared columns.

Each problem is reported with its path, for example `columns.amount.min`.

## SandboxBank contracts and the defect catalogue

`contracts/sandboxbank/` holds the contracts RAI uses in the benchmark. Every Protocol §4 defect class is caught by at least one rule. The rule engine (B-09 to B-11) turns this mapping into tests.

| Defect | Caught by |
| --- | --- |
| D01 duplicate key within batch | `txn_id.unique` |
| D02 duplicate key across batches | `unique_across_batches:txn_id` |
| D03 missing required value | `<column>.required` |
| D04 type error | `amount.type` |
| D05 out-of-range value | `amount.range` (negative) and `amount.max_lookup` (over the account's limit) |
| D06 broken reference | `account_id.references` |
| D07 column renamed | `schema` (breaking) |
| D08 column dropped | `schema` (breaking) |
| D09 column type changed | `schema` (breaking type change) and `amount.type` |
| D10 late-arriving rows | `freshness` |
| D11 stale feed | `unique_across_batches:txn_id` and `freshness` |
| D12 join fan-out | `merchants` contract: `merchant_id.unique` (primary key) |
