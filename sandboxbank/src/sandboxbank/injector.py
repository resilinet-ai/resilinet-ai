"""Seeded data-defect injector (B-07, Protocol section 4).

Each function takes one clean ``transactions_raw`` batch and returns the defective
batch plus the exact ground truth: which data rows changed (0-based, in the
output file), which columns, and the transaction keys involved. Defects are
injected only here, never by the generator, so a clean batch is always clean.

The twelve classes and their rates are fixed by the protocol:

==== ============================ ================================= ===============
ID   Class                        How it is seeded                  Rates
==== ============================ ================================= ===============
D01  Duplicate key within batch   copy rows, keep txn_id            0.01%, 0.1%, 1%
D02  Duplicate key across batches re-emit delivered earlier rows   0.01%, 0.1%, 1%
D03  Missing required value       set a required field to empty     0.01%, 0.1%, 1%
D04  Type error                   write text into ``amount``        0.01%, 0.1%, 1%
D05  Out-of-range value           negative or over-limit amount     0.01%, 0.1%, 1%
D06  Broken reference             account_id not in accounts        0.01%, 0.1%, 1%
D07  Column renamed               rename one column                 whole batch
D08  Column dropped               remove one column                 whole batch
D09  Column type changed          decimal amount to float or text   whole batch
D10  Late-arriving rows           created_at before freshness       1%, 5%
D11  Stale feed                   previous delivery re-sent as is   whole batch
D12  Join fan-out                 duplicate keys in merchants dim   0.1%, 1%
==== ============================ ================================= ===============

**Per pipeline** (author's decision, 2026-11-24: all twelve classes on every pipeline,
adapted). A :class:`Profile` names the columns each class acts on:

* **P1** ``transactions_raw``: as above.
* **P2** ``fact_transaction``: the same classes on the warehouse load; D03, D07 and D08
  use ``committed_at`` and ``business_date`` where P1 uses ``status``.
* **P3** ``fraud_features``: D04 writes text into ``score``; D05 makes ``amount``
  negative or ``score`` fall outside [0, 1]; D06 puts a ``merchant_category`` outside
  the merchants catalogue; D10 makes ``scored_at`` late; D12 fans a transaction out to
  two account-dimension rows (the same ``txn_id`` with a different ``account_type``).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sandboxbank.generator import format_timestamp

Header = list[str]
Rows = list[list[str]]

# A row is "late" if created_at is earlier than its batch window start minus this.
FRESHNESS_WINDOW = timedelta(hours=1)

WHOLE_BATCH = None
RATES: dict[str, tuple[float | None, ...]] = {
    "D01": (0.0001, 0.001, 0.01),
    "D02": (0.0001, 0.001, 0.01),
    "D03": (0.0001, 0.001, 0.01),
    "D04": (0.0001, 0.001, 0.01),
    "D05": (0.0001, 0.001, 0.01),
    "D06": (0.0001, 0.001, 0.01),
    "D07": (WHOLE_BATCH,),
    "D08": (WHOLE_BATCH,),
    "D09": (WHOLE_BATCH,),
    "D10": (0.01, 0.05),
    "D11": (WHOLE_BATCH,),
    "D12": (0.001, 0.01),
}
CLASS_NAMES = {
    "D01": "duplicate key within batch",
    "D02": "duplicate key across batches",
    "D03": "missing required value",
    "D04": "type error",
    "D05": "out-of-range value",
    "D06": "broken reference",
    "D07": "column renamed",
    "D08": "column dropped",
    "D09": "column type changed",
    "D10": "late-arriving rows",
    "D11": "stale feed",
    "D12": "join fan-out",
}

REQUIRED_FOR_D03 = ("txn_id", "account_id", "amount", "currency", "status", "created_at")
RENAMES_FOR_D07 = {
    "amount": "amt",
    "created_at": "created_ts",
    "account_id": "acct_id",
    "merchant_id": "merchant",
    "status": "txn_status",
}
DROPPABLE_FOR_D08 = ("currency", "status", "channel", "direction", "merchant_id", "is_fraud")
TEXT_AMOUNTS_FOR_D04 = ("N/A", "abc", "12.34.5", "1,234.50", "")
ACCOUNT_TYPES_FOR_D12 = ("checking", "savings", "business")


@dataclass(frozen=True, slots=True)
class Profile:
    """The columns each defect class acts on, for one pipeline's delivered table."""

    pipeline: str
    required: tuple[str, ...]  # D03
    renames: dict[str, str]  # D07
    droppable: tuple[str, ...]  # D08
    type_error_column: str  # D04
    type_change_column: str  # D09
    timestamp_column: str  # D10


P1 = Profile("P1", REQUIRED_FOR_D03, RENAMES_FOR_D07, DROPPABLE_FOR_D08, "amount", "amount",
             "created_at")  # fmt: skip
P2 = Profile(
    "P2",
    ("txn_id", "account_id", "amount", "currency", "created_at", "committed_at"),
    {"amount": "amt", "created_at": "created_ts", "account_id": "acct_id",
     "merchant_id": "merchant", "committed_at": "commit_ts"},
    ("currency", "channel", "direction", "merchant_id", "is_fraud", "business_date"),
    "amount", "amount", "created_at",
)  # fmt: skip
P3 = Profile(
    "P3",
    ("txn_id", "amount", "channel", "score", "model_recipe_sha256", "scored_at"),
    {"score": "fraud_score", "amount": "amt", "merchant_category": "mcc_category",
     "account_type": "acct_type", "scored_at": "scored_ts"},
    ("amount_to_limit", "channel", "account_type", "merchant_category", "is_fraud", "status"),
    "score", "amount", "scored_at",
)  # fmt: skip
PROFILES = {"P1": P1, "P2": P2, "P3": P3}


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a defect may need besides the batch itself.

    ``previous_header``/``previous_rows`` are the previous batch *as delivered* (D11
    re-delivers it unchanged). ``reemit_rows`` are unaffected rows of the most recent
    delivered batch with the standard columns (D02 re-emits some of them), so a
    re-emitted row is a true duplicate of data the arms have seen and carries no
    second, unrecorded defect.
    """

    window_start: datetime
    previous_header: Header
    previous_rows: Rows
    account_limits: dict[str, Decimal]
    merchants_header: Header
    merchants_rows: Rows
    reemit_rows: Rows = field(default_factory=list)
    profile: Profile = P1


@dataclass(slots=True)
class Injection:
    header: Header
    rows: Rows
    rows_affected: list[int] = field(default_factory=list)  # 0-based data rows of the output
    columns: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)  # txn_ids (or merchant_ids for D12)
    whole_batch: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    merchants_rows: Rows | None = None  # D12: the dimension delivered with this batch


def instance_count(rate: float, total: int) -> int:
    """Rows to affect at ``rate``; never zero, so every defective batch has a defect."""
    return max(1, min(total, round(rate * total)))


def _pick(rng: random.Random, total: int, rate: float) -> list[int]:
    return sorted(rng.sample(range(total), instance_count(rate, total)))


def _copy(rows: Rows) -> Rows:
    return [list(row) for row in rows]


def _insert_flagged(rng: random.Random, rows: Rows, extra: Rows) -> tuple[Rows, list[int]]:
    """Insert ``extra`` rows at random positions; return new rows and their indices."""
    tagged: list[tuple[list[str], bool]] = [(row, False) for row in rows]
    for row in extra:
        tagged.insert(rng.randrange(len(tagged) + 1), (row, True))
    return [row for row, _ in tagged], [i for i, (_, flag) in enumerate(tagged) if flag]


def d01(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    sources = _pick(rng, len(rows), rate)
    out, flagged = _insert_flagged(rng, _copy(rows), [list(rows[i]) for i in sources])
    key = header.index("txn_id")
    return Injection(header, out, flagged, ["txn_id"], [out[i][key] for i in flagged])


def d02(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    if not ctx.reemit_rows:
        raise ValueError("D02 needs rows from an earlier delivered batch")
    count = min(instance_count(rate, len(rows)), len(ctx.reemit_rows))
    sources = sorted(rng.sample(range(len(ctx.reemit_rows)), count))
    out, flagged = _insert_flagged(rng, _copy(rows), [list(ctx.reemit_rows[i]) for i in sources])
    key = header.index("txn_id")
    return Injection(header, out, flagged, ["txn_id"], [out[i][key] for i in flagged])


def d03(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    column = rng.choice(ctx.profile.required)
    col, key = header.index(column), header.index("txn_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    keys = [out[i][key] for i in targets]  # before blanking, in case the column is txn_id
    for i in targets:
        out[i][col] = ""
    return Injection(header, out, targets, [column], keys)


def d04(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    column = ctx.profile.type_error_column
    col, key = header.index(column), header.index("txn_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    for i in targets:
        out[i][col] = rng.choice(TEXT_AMOUNTS_FOR_D04[:-1])  # non-empty: empty would be D03
    return Injection(header, out, targets, [column], [out[i][key] for i in targets])


def d05(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    if ctx.profile.pipeline == "P3":
        return _d05_features(rng, header, rows, rate)
    col, key, acct = header.index("amount"), header.index("txn_id"), header.index("account_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    kinds = {}
    for i in targets:
        if rng.random() < 0.5:
            out[i][col] = f"-{Decimal(out[i][col]):.2f}"
            kinds[out[i][key]] = "negative"
        else:
            limit = ctx.account_limits[out[i][acct]]
            out[i][col] = f"{limit + Decimal(rng.randrange(1, 100_000)) / 100:.2f}"
            kinds[out[i][key]] = "over_limit"
    return Injection(
        header, out, targets, ["amount"], [out[i][key] for i in targets], detail={"kind": kinds}
    )


def _d05_features(rng: random.Random, header: Header, rows: Rows, rate: float) -> Injection:
    """P3: a negative amount, or a score outside [0, 1]."""
    amount, score, key = header.index("amount"), header.index("score"), header.index("txn_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    kinds = {}
    for i in targets:
        if rng.random() < 0.5:
            out[i][amount] = f"-{Decimal(out[i][amount]):.2f}"
            kinds[out[i][key]] = "negative"
        else:
            above = rng.random() < 0.5
            step = rng.randrange(1, 1_000) / 1_000
            out[i][score] = f"{1 + step:.6f}" if above else f"{-step:.6f}"
            kinds[out[i][key]] = "score_out_of_range"
    columns = sorted({"amount" if kind == "negative" else "score" for kind in kinds.values()})
    return Injection(header, out, targets, columns, [out[i][key] for i in targets],
                     detail={"kind": kinds})  # fmt: skip


def d06(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    if ctx.profile.pipeline == "P3":
        return _d06_features(rng, header, rows, rate)
    col, key = header.index("account_id"), header.index("txn_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    for i in targets:
        # ACC-9xxxxxx is never generated: the generator numbers accounts from 1.
        out[i][col] = f"ACC-9{rng.randrange(1_000_000):06d}"
    return Injection(header, out, targets, ["account_id"], [out[i][key] for i in targets])


def _d06_features(rng: random.Random, header: Header, rows: Rows, rate: float) -> Injection:
    """P3: a merchant_category outside the merchants catalogue, on rows with a merchant."""
    col, key, has = header.index("merchant_category"), header.index("txn_id"), header.index(
        "has_merchant")  # fmt: skip
    out = _copy(rows)
    candidates = [i for i, row in enumerate(out) if row[has] == "1"]
    if not candidates:
        raise ValueError("D06 on P3 needs rows with a merchant")
    count = min(instance_count(rate, len(out)), len(candidates))
    targets = sorted(rng.sample(candidates, count))
    for i in targets:
        # No catalogue category has a digit: these never match one.
        out[i][col] = f"category_9{rng.randrange(1_000):03d}"
    return Injection(header, out, targets, ["merchant_category"], [out[i][key] for i in targets])


def d07(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    renames = ctx.profile.renames
    old = rng.choice(sorted(renames))
    new_header = [renames[name] if name == old else name for name in header]
    return Injection(
        new_header,
        _copy(rows),
        columns=[old],
        whole_batch=True,
        detail={"renamed_from": old, "renamed_to": renames[old]},
    )


def d08(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    column = rng.choice(ctx.profile.droppable)
    col = header.index(column)
    new_rows = [row[:col] + row[col + 1 :] for row in rows]
    return Injection(
        [name for name in header if name != column],
        new_rows,
        columns=[column],
        whole_batch=True,
        detail={"dropped": column},
    )


def d09(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    column = ctx.profile.type_change_column
    col = header.index(column)
    variant = rng.choice(("float", "string"))
    out = _copy(rows)
    for row in out:
        value = Decimal(row[col])
        # float: binary rounding noise, as a lossy float pipeline would write it;
        # string: currency-formatted text.
        row[col] = f"{float(value):.17g}" if variant == "float" else f"USD {value:,.2f}"
    return Injection(header, out, columns=[column], whole_batch=True, detail={"became": variant})


def d10(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    column = ctx.profile.timestamp_column
    col, key = header.index(column), header.index("txn_id")
    out = _copy(rows)
    targets = _pick(rng, len(out), rate)
    cutoff = ctx.window_start - FRESHNESS_WINDOW
    for i in targets:
        late = cutoff - timedelta(milliseconds=rng.randrange(1, 6 * 3_600_000))
        out[i][col] = format_timestamp(late)
    return Injection(
        header,
        out,
        targets,
        [column],
        [out[i][key] for i in targets],
        detail={"freshness_window_seconds": int(FRESHNESS_WINDOW.total_seconds())},
    )


def d11(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    return Injection(
        list(ctx.previous_header),
        _copy(ctx.previous_rows),
        whole_batch=True,
        detail={"redelivered": "previous delivered batch, unchanged"},
    )


def d12(rng: random.Random, header: Header, rows: Rows, rate: float, ctx: Context) -> Injection:
    if ctx.profile.pipeline == "P3":
        return _d12_features(rng, header, rows, rate)
    key_col = ctx.merchants_header.index("merchant_id")
    dims = _copy(ctx.merchants_rows)
    duplicated = {dims[i][key_col] for i in _pick(rng, len(dims), rate)}
    extra = [list(row) for row in dims if row[key_col] in duplicated]
    merchants_out, _ = _insert_flagged(rng, dims, extra)
    merchant_col, txn_col = header.index("merchant_id"), header.index("txn_id")
    affected = [i for i, row in enumerate(rows) if row[merchant_col] in duplicated]
    return Injection(
        header,
        _copy(rows),
        affected,
        ["merchant_id"],
        sorted(duplicated),
        detail={"fanned_out_txn_ids": [rows[i][txn_col] for i in affected]},
        merchants_rows=merchants_out,
    )


def _d12_features(rng: random.Random, header: Header, rows: Rows, rate: float) -> Injection:
    """P3: a transaction joined to two account-dimension rows, so it appears twice, the
    second time with a different account_type. The extra rows are the defect."""
    kind, key = header.index("account_type"), header.index("txn_id")
    sources = _pick(rng, len(rows), rate)
    extra = []
    for i in sources:
        copy = list(rows[i])
        others = [t for t in ACCOUNT_TYPES_FOR_D12 if t != copy[kind]]
        copy[kind] = rng.choice(others)
        extra.append(copy)
    out, flagged = _insert_flagged(rng, _copy(rows), extra)
    keys = [out[i][key] for i in flagged]
    # Both columns are wrong in the extra rows: the key is duplicated, and account_type
    # disagrees with the original row.
    return Injection(header, out, flagged, ["txn_id", "account_type"], keys,
                     detail={"fanned_out_txn_ids": keys, "dimension": "accounts"})  # fmt: skip


Defect = Callable[[random.Random, Header, Rows, float, Context], Injection]
DEFECTS: dict[str, Defect] = {
    "D01": d01,
    "D02": d02,
    "D03": d03,
    "D04": d04,
    "D05": d05,
    "D06": d06,
    "D07": d07,
    "D08": d08,
    "D09": d09,
    "D10": d10,
    "D11": d11,
    "D12": d12,
}


def inject(
    defect_class: str,
    rng: random.Random,
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    rate: float | None,
    ctx: Context,
) -> Injection:
    if defect_class not in DEFECTS:
        raise ValueError(f"unknown defect class {defect_class}")
    if rate not in RATES[defect_class]:
        raise ValueError(f"rate {rate} is not a protocol rate for {defect_class}")
    if not rows:
        raise ValueError("cannot inject into an empty batch")
    return DEFECTS[defect_class](rng, list(header), [list(r) for r in rows], rate or 1.0, ctx)
