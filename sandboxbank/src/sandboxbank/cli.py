"""Command line for SandboxBank.

sandboxbank generate --seed 42 --out data/sandboxbank --batches 3
sandboxbank verify data/sandboxbank
sandboxbank load-reference --data data/sandboxbank        (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank canaries write --accounts data/sandboxbank/accounts.csv --seed 7 --out data/canaries
sandboxbank canaries load --dir data/canaries               (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank post --data data/sandboxbank --api http://127.0.0.1:18080
sandboxbank serve --port 8080                              (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank fixtures --data-seed 1 --injector-seed 2 --out data/fixtures
sandboxbank seal data/fixtures [--expect <sha256>]
sandboxbank etl [--once]          (needs SANDBOXBANK_LEDGER_DSN and SANDBOXBANK_ANALYTICS_DSN)
sandboxbank seed --data data/sandboxbank --log acks/acks.csv --rate 10
sandboxbank verify-acks acks/acks.csv
sandboxbank observe --log obs/observations.csv               (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank verify-observations obs/observations.csv
sandboxbank observed-rto obs/observations.csv --onset <ISO 8601> --reverted <ISO 8601>
sandboxbank model card --out sandboxbank/MODEL_CARD.md      (needs the `model` extra)
sandboxbank model score --feed data/slice/feed --batch B000001 --out scores.csv
sandboxbank model features --feed data/slice/feed --batch B000001 --out features.csv
sandboxbank model serve --port 8081                         (the model-api, FR-09.3)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sandboxbank.dataset import verify_dataset, write_dataset
from sandboxbank.generator import GeneratorConfig

DSN_ENV = "SANDBOXBANK_LEDGER_DSN"
ANALYTICS_DSN_ENV = "SANDBOXBANK_ANALYTICS_DSN"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandboxbank", description="SandboxBank synthetic payments system (synthetic only)."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Write a seeded dataset and its manifest.")
    generate.add_argument("--seed", type=int, required=True, help="0-99999 dev, 100000-199999 eval")
    generate.add_argument("--out", type=Path, required=True, help="Output directory")
    generate.add_argument("--accounts", type=int, default=5_000)
    generate.add_argument("--merchants", type=int, default=500)
    generate.add_argument("--batches", type=int, default=1)
    generate.add_argument("--rows", type=int, default=100_000, help="Rows per batch")
    generate.add_argument("--window", type=int, default=900, help="Batch window in seconds")
    generate.add_argument("--force", action="store_true", help="Overwrite a non-empty directory")

    verify = commands.add_parser("verify", help="Check files against manifest.json.")
    verify.add_argument("out", type=Path, help="Dataset directory")

    load = commands.add_parser("load-reference", help="Load accounts and merchants into ledger-db.")
    load.add_argument("--data", type=Path, required=True, help="Dataset directory")

    post = commands.add_parser("post", help="Post transaction batches through the payments-api.")
    post.add_argument("--data", type=Path, required=True, help="Dataset directory")
    post.add_argument("--api", default="http://127.0.0.1:18080", help="payments-api base URL")
    post.add_argument("--batch", action="append", help="Batch ID such as B000000 (repeatable)")
    post.add_argument("--chunk", type=int, default=1_000, help="Rows per request (max 1000)")

    serve = commands.add_parser("serve", help="Run the payments-api.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    fixtures = commands.add_parser(
        "fixtures", help="Write a known-defect fixture set with a sealed answer key."
    )
    fixtures.add_argument("--data-seed", type=int, required=True, help="Generator seed")
    fixtures.add_argument("--injector-seed", type=int, required=True, help="Injector seed")
    fixtures.add_argument("--out", type=Path, required=True, help="Output directory")
    fixtures.add_argument("--accounts", type=int, default=5_000)
    fixtures.add_argument("--merchants", type=int, default=500)
    fixtures.add_argument("--rows", type=int, default=100_000, help="Rows per batch")
    fixtures.add_argument("--per-class", type=int, default=12, help="Batches per defect class")
    fixtures.add_argument("--clean", type=int, default=36, help="Clean control batches")
    fixtures.add_argument("--force", action="store_true", help="Overwrite a non-empty directory")

    slice_ = commands.add_parser(
        "slice", help="First runnable slice: a clean batch, then one re-sending 0.1%% of it."
    )
    slice_.add_argument("--data-seed", type=int, default=7, help="Generator seed")
    slice_.add_argument("--injector-seed", type=int, default=8, help="Injector seed")
    slice_.add_argument("--out", type=Path, required=True, help="Output directory")
    slice_.add_argument("--rows", type=int, default=100_000, help="Rows per batch")
    slice_.add_argument("--force", action="store_true", help="Replace a non-empty directory")

    seal = commands.add_parser("seal", help="Check a fixture set's answer key against its seal.")
    seal.add_argument("out", type=Path, help="Fixture directory")
    seal.add_argument("--expect", help="Publicly committed SHA-256 to compare with")

    etl = commands.add_parser("etl", help="Run the P2 settlement ETL (ledger to warehouse).")
    etl.add_argument("--once", action="store_true", help="Run one cycle and exit")
    etl.add_argument("--interval", type=float, default=60.0, help="Seconds between cycles")
    etl.add_argument("--chunk", type=int, default=10_000, help="Ledger rows per committed chunk")
    etl.add_argument(
        "--lag", type=float, default=5.0, help="Only read rows committed this long ago"
    )

    features = commands.add_parser(
        "features", help="Run the P3 fraud feature pipeline (warehouse to feature table)."
    )
    features.add_argument("--once", action="store_true", help="Run one cycle and exit")
    features.add_argument("--interval", type=float, default=60.0, help="Seconds between cycles")
    features.add_argument(
        "--chunk",
        type=int,
        default=5_000,
        help="Warehouse rows per scored, committed chunk (at most 5,000)",
    )
    features.add_argument("--model-api", default="http://127.0.0.1:18081",
                          help="The model-api's base URL")  # fmt: skip

    seed = commands.add_parser(
        "seed", help="Post transactions one by one at a steady rate, logging every answer."
    )
    seed.add_argument("--data", type=Path, required=True, help="Dataset directory")
    seed.add_argument("--log", type=Path, required=True, help="Seeding log (CSV, appended)")
    seed.add_argument("--api", default="http://127.0.0.1:18080", help="payments-api base URL")
    seed.add_argument("--batch", action="append", help="Batch ID such as B000000 (repeatable)")
    seed.add_argument("--rate", type=float, default=10.0, help="Transactions per second")
    seed.add_argument("--limit", type=int, help="Stop after this many transactions")

    acks = commands.add_parser("verify-acks", help="Check a seeding log's hash chain.")
    acks.add_argument("log", type=Path, help="Seeding log")

    observe = commands.add_parser(
        "observe", help="Sample the payments-api and ledger every 250 ms: RTO ground truth (B-44)."
    )
    observe.add_argument("--api", default="http://127.0.0.1:18080", help="payments-api base URL")
    observe.add_argument("--log", type=Path, required=True, help="Observation log (CSV, appended)")
    observe.add_argument("--interval", type=float, default=0.25, help="Seconds between samples")
    observe.add_argument("--timeout", type=float, default=2.0, help="Seconds each check may take")
    observe.add_argument("--samples", type=int, help="Stop after this many samples")

    observations = commands.add_parser(
        "verify-observations", help="Check an observation log's hash chain."
    )
    observations.add_argument("log", type=Path, help="Observation log")

    observed = commands.add_parser(
        "observed-rto", help="Ground-truth RTO of one injection from an observation log."
    )
    observed.add_argument("log", type=Path, help="Observation log")
    observed.add_argument("--onset", required=True, help="Fault onset, ISO 8601 with a zone")
    observed.add_argument("--reverted", required=True, help="Last revert, ISO 8601 with a zone")
    observed.add_argument("--hold", type=float, default=2.0,
                          help="Seconds healthy before it counts (stable_checks - 1)")  # fmt: skip
    observed.add_argument("--window", type=float, help="Seconds after the revert to look")

    canaries = commands.add_parser(
        "canaries", help="Fake customers for RAI's privacy sentinel (FR-05.1)."
    )
    canary_commands = canaries.add_subparsers(dest="canary_command", required=True)
    write = canary_commands.add_parser(
        "write", help="Write customers.csv and the answer key canaries.json."
    )
    write.add_argument("--accounts", type=Path, required=True,
                       help="The dataset's accounts.csv (canaries use its customers)")  # fmt: skip
    write.add_argument("--seed", type=int, required=True, help="Seed of the canaries' stream")
    write.add_argument("--count", type=int, default=5, help="How many canaries (1-50)")
    write.add_argument("--out", type=Path, required=True, help="Folder for the two files")
    load_canaries = canary_commands.add_parser("load", help="Load customers.csv into ledger-db.")
    load_canaries.add_argument("--dir", type=Path, required=True,
                               help="Folder written by `canaries write`")  # fmt: skip

    model = commands.add_parser("model", help="The fraud model (ADR-005; the `model` extra).")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    card = model_commands.add_parser("card", help="Retrain, evaluate and write the model card.")
    card.add_argument("--out", type=Path, required=True, help="Model card (Markdown)")
    score = model_commands.add_parser(
        "score", help="Retrain, then score one batch of a feed (txn_id, score, is_fraud)."
    )
    feed_help = "Feed folder: accounts.csv, merchants.csv, transactions_raw/"
    score.add_argument("--feed", type=Path, required=True, help=feed_help)
    score.add_argument("--batch", required=True, help="Batch ID such as B000001, or a CSV path")
    score.add_argument("--out", type=Path, required=True, help="Scores (CSV)")
    table = model_commands.add_parser(
        "features",
        help="Write one batch as the model's named inputs (txn_id, the features, is_fraud).",
    )
    table.add_argument("--feed", type=Path, required=True, help=feed_help)
    table.add_argument("--batch", required=True, help="Batch ID such as B000001, or a CSV path")
    table.add_argument("--out", type=Path, required=True, help="Feature table (CSV)")
    serve_model = model_commands.add_parser(
        "serve", help="Run the model-api: trains, then scores named feature records (FR-09.3)."
    )
    serve_model.add_argument("--host", default="127.0.0.1")
    serve_model.add_argument("--port", type=int, default=8081)
    return parser


def _error(message: object, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _generate(args: argparse.Namespace) -> int:
    try:
        config = GeneratorConfig(
            seed=args.seed,
            accounts=args.accounts,
            merchants=args.merchants,
            batches=args.batches,
            rows_per_batch=args.rows,
            batch_window_seconds=args.window,
        )
        manifest = write_dataset(config, args.out, force=args.force)
    except (ValueError, FileExistsError) as error:
        return _error(error)
    rows = sum(entry["rows"] for entry in manifest["files"])
    print(
        f"Wrote {len(manifest['files'])} files, {rows} rows, "
        f"seed {config.seed} ({config.purpose}) to {args.out}"
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    problems = verify_dataset(args.out)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{args.out}: all files match the manifest")
    return 0


def _load_reference(args: argparse.Namespace) -> int:
    import psycopg

    from sandboxbank.loader import load_reference

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        return _error(f"set {DSN_ENV} to the ledger-db connection string")
    with psycopg.connect(dsn) as conn:
        inserted = load_reference(conn, args.data)
    print(", ".join(f"{table}: {count} new" for table, count in inserted.items()))
    return 0


def _post(args: argparse.Namespace) -> int:
    from sandboxbank.loader import post_batches

    folder = args.data / "transactions_raw"
    files = (
        [folder / f"{batch}.csv" for batch in args.batch]
        if args.batch
        else sorted(folder.glob("B*.csv"))
    )
    missing = [str(path) for path in files if not path.is_file()]
    if missing or not files:
        return _error(f"batch files not found: {', '.join(missing) or folder}")
    try:
        totals = post_batches(args.api, files, chunk=args.chunk)
    except (ValueError, OSError) as error:
        return _error(error)
    print(", ".join(f"{outcome}: {count}" for outcome, count in sorted(totals.items())))
    return 0


def _fixtures(args: argparse.Namespace) -> int:
    from sandboxbank.fixtures import build_fixtures

    try:
        manifest = build_fixtures(
            args.out,
            data_seed=args.data_seed,
            injector_seed=args.injector_seed,
            accounts=args.accounts,
            merchants=args.merchants,
            rows_per_batch=args.rows,
            batches_per_class=args.per_class,
            clean=args.clean,
            force=args.force,
        )
    except (ValueError, FileExistsError) as error:
        return _error(error)
    print(
        f"Wrote {manifest['config']['batches']} batches "
        f"({manifest['seed_purpose']}) to {args.out}\n"
        f"Answer key SEAL: {manifest['answer_key_sha256']}\n"
        "Commit the SEAL publicly before any arm runs; keep answer_key/ away from the arms."
    )
    return 0


def _slice(args: argparse.Namespace) -> int:
    from sandboxbank.fixtures import build_slice

    try:
        result = build_slice(
            args.out,
            data_seed=args.data_seed,
            injector_seed=args.injector_seed,
            rows_per_batch=args.rows,
            force=args.force,
        )
    except (ValueError, FileExistsError) as error:
        return _error(error)
    defective = result["batches"][1]
    print(
        f"Wrote clean batch {result['batches'][0]['batch_id']} and "
        f"{defective['batch_id']} with {len(defective['rows'])} re-sent rows (D02) to {args.out}"
    )
    return 0


def _seal(args: argparse.Namespace) -> int:
    from sandboxbank.fixtures import verify_seal

    if verify_seal(args.out, args.expect):
        print(f"{args.out}: answer key matches its seal")
        return 0
    print(f"{args.out}: answer key does NOT match its seal", file=sys.stderr)
    return 1


def _features(args: argparse.Namespace) -> int:
    import threading

    from sandboxbank.etl import main_loop_signals
    from sandboxbank.features import run_service

    analytics_dsn = os.environ.get(ANALYTICS_DSN_ENV)
    if not analytics_dsn:
        return _error(f"set {ANALYTICS_DSN_ENV}")
    if not 1 <= args.chunk <= 5_000 or args.interval <= 0:
        return _error("--chunk must be 1 to 5,000 and --interval above 0")
    stop = threading.Event()
    if not args.once:  # pragma: no cover - long-running service
        main_loop_signals(stop)
    return run_service(
        analytics_dsn,
        args.model_api.rstrip("/") + "/v1/score",
        once=args.once,
        interval=args.interval,
        chunk_size=args.chunk,
        stop=stop,
    )


def _etl(args: argparse.Namespace) -> int:
    import threading

    from sandboxbank.etl import main_loop_signals, run_service

    ledger_dsn, analytics_dsn = os.environ.get(DSN_ENV), os.environ.get(ANALYTICS_DSN_ENV)
    if not ledger_dsn or not analytics_dsn:
        return _error(f"set {DSN_ENV} and {ANALYTICS_DSN_ENV}")
    if args.chunk < 1 or args.interval <= 0 or args.lag < 0:
        return _error("--chunk must be at least 1, --interval above 0 and --lag at least 0")
    stop = threading.Event()
    if not args.once:  # pragma: no cover - long-running service
        main_loop_signals(stop)
    return run_service(
        ledger_dsn,
        analytics_dsn,
        once=args.once,
        interval=args.interval,
        chunk_size=args.chunk,
        lag_seconds=args.lag,
        stop=stop,
    )


def _serve(args: argparse.Namespace) -> int:  # pragma: no cover - starts a server
    import uvicorn

    uvicorn.run("sandboxbank.api:app", host=args.host, port=args.port)
    return 0


def _seed(args: argparse.Namespace) -> int:
    from itertools import chain, islice

    from sandboxbank.loader import read_rows
    from sandboxbank.seeder import AckLog, seed

    folder = args.data / "transactions_raw"
    files = (
        [folder / f"{batch}.csv" for batch in args.batch]
        if args.batch
        else sorted(folder.glob("B*.csv"))
    )
    missing = [str(path) for path in files if not path.is_file()]
    if missing or not files:
        return _error(f"batch files not found: {', '.join(missing) or folder}")
    rows = chain.from_iterable(read_rows(path) for path in files)
    try:
        log = AckLog(args.log)
        totals = seed(args.api, islice(rows, args.limit), log, rate=args.rate)
    except (ValueError, OSError) as error:
        return _error(error)
    print(", ".join(f"{outcome}: {count}" for outcome, count in sorted(totals.items())))
    print(f"log: {args.log} ({log.count} rows, chain head {log.head})")
    return 0


def _verify_acks(args: argparse.Namespace) -> int:
    from sandboxbank.seeder import verify

    try:
        rows, head = verify(args.log)
    except (ValueError, OSError) as error:
        return _error(error, code=1)
    print(f"intact: {rows} rows, chain head {head}")
    return 0


def _observe(args: argparse.Namespace) -> int:  # pragma: no cover - runs until stopped
    from sandboxbank.observer import LedgerQuery, ObservationLog, observe

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        return _error(f"set {DSN_ENV} to the ledger-db connection string")
    ledger = LedgerQuery(dsn)
    try:
        log = ObservationLog(args.log)
        observe(args.api, ledger, log, interval=args.interval, timeout=args.timeout,
                samples=args.samples)  # fmt: skip
    except (ValueError, OSError) as error:
        return _error(error)
    except KeyboardInterrupt:
        pass
    finally:
        ledger.close()
    print(f"log: {args.log} ({log.count} samples, chain head {log.head})")
    return 0


def _verify_observations(args: argparse.Namespace) -> int:
    from sandboxbank.observer import verify

    try:
        samples, head = verify(args.log)
    except (ValueError, OSError) as error:
        return _error(error, code=1)
    print(f"intact: {samples} samples, chain head {head}")
    return 0


def _zoned(text: str) -> datetime:
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{text!r} has no time zone")
    return moment


def _observed_rto(args: argparse.Namespace) -> int:
    from sandboxbank.observer import read, restoration

    try:
        truth = restoration(read(args.log), _zoned(args.onset), _zoned(args.reverted),
                            hold=args.hold, window=args.window)  # fmt: skip
    except (ValueError, OSError) as error:
        return _error(error)
    if not truth.observed:
        print(f"unobserved: {truth.reason}")
        return 1
    if truth.restored_at is None:
        print(f"not restored: {truth.reason}")
        return 0
    print(f"RTO {truth.seconds} s (restored at {truth.restored_at.isoformat()})")
    return 0


def _canaries(args: argparse.Namespace) -> int:
    import csv

    from sandboxbank import canaries

    if args.canary_command == "write":
        try:
            with args.accounts.open(newline="", encoding="utf-8") as handle:
                customers = [row["customer_id"] for row in csv.DictReader(handle)]
            made = canaries.generate_canaries(args.seed, customers, args.count)
        except (OSError, KeyError, ValueError) as error:
            return _error(error)
        canaries.write_canaries(made, args.seed, args.out)
        print(f"{len(made)} canary customers written to {args.out} "
              f"({canaries.CUSTOMERS_FILE}; answer key {canaries.KEY_FILE})")  # fmt: skip
        return 0
    import psycopg

    from sandboxbank.loader import load_canaries

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        return _error(f"set {DSN_ENV} to the ledger-db connection string")
    try:
        with psycopg.connect(dsn) as conn:
            added = load_canaries(conn, args.dir)
    except (OSError, KeyError) as error:
        return _error(error)
    print(f"customers: {added} new")
    return 0


def _model(args: argparse.Namespace) -> int:
    import csv

    try:
        from sandboxbank import model
    except ImportError:  # pragma: no cover - the extra is installed in CI
        return _error("the fraud model needs the `model` extra: "
                      "uv sync --package rai-sandboxbank --extra model")  # fmt: skip
    if args.model_command == "serve":
        import uvicorn

        from sandboxbank.model_api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    try:
        trained = None if args.model_command == "features" else model.train()
        if args.model_command == "card":
            assert trained is not None  # noqa: S101 - trained above
            evaluation, fingerprint = model.holdout(trained)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(model.card(trained, evaluation, fingerprint), encoding="utf-8",
                                newline="\n")  # fmt: skip
            print(f"model card written to {args.out}: hold-out ROC AUC "
                  f"{evaluation.roc_auc:.4f} over {evaluation.n:,} transactions "
                  f"({evaluation.positives:,} fraud)")  # fmt: skip
            return 0
        feed = args.feed
        batch = Path(args.batch)
        if not batch.suffix:
            batch = feed / "transactions_raw" / f"{args.batch}.csv"

        def rows(path: Path) -> list[dict[str, str]]:
            with path.open(newline="", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))

        reference = model.Reference.from_rows(rows(feed / "accounts.csv"),
                                              rows(feed / "merchants.csv"))  # fmt: skip
        transactions = rows(batch)
        if args.model_command == "features":
            return _feature_table(model, transactions, reference, args.out)
        assert trained is not None  # noqa: S101 - trained above
        scores = trained.score(transactions, reference)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["txn_id", "score", "is_fraud"])
            for row, value in zip(transactions, scores, strict=True):
                writer.writerow([row["txn_id"], f"{value:.6f}", row.get("is_fraud", "")])
    except (OSError, KeyError, ValueError) as error:
        return _error(error)
    print(f"{len(scores):,} transactions scored into {args.out}")
    return 0


def _feature_table(
    model: Any, transactions: list[dict[str, str]], reference: Any, out: Path
) -> int:
    """``model features``: the batch as named inputs; a missing value is an empty cell."""
    import csv

    names = [name for name, _ in model.FEATURES]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["txn_id", *names, "is_fraud"])
        for row in transactions:
            record = model.feature_record(row, reference)
            cells = ["" if record[n] is None else
                     (f"{record[n]:.6f}" if isinstance(record[n], float) else record[n])
                     for n in names]  # fmt: skip
            writer.writerow([row["txn_id"], *cells, row.get("is_fraud", "")])
    print(f"{len(transactions):,} transactions written as model inputs to {out}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "generate": _generate,
    "verify": _verify,
    "load-reference": _load_reference,
    "post": _post,
    "serve": _serve,
    "etl": _etl,
    "features": _features,
    "fixtures": _fixtures,
    "seal": _seal,
    "slice": _slice,
    "seed": _seed,
    "verify-acks": _verify_acks,
    "observe": _observe,
    "verify-observations": _verify_observations,
    "observed-rto": _observed_rto,
    "canaries": _canaries,
    "model": _model,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
