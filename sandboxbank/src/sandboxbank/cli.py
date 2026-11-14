"""Command line for SandboxBank.

sandboxbank generate --seed 42 --out data/sandboxbank --batches 3
sandboxbank verify data/sandboxbank
sandboxbank load-reference --data data/sandboxbank        (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank post --data data/sandboxbank --api http://127.0.0.1:18080
sandboxbank serve --port 8080                              (needs SANDBOXBANK_LEDGER_DSN)
sandboxbank fixtures --data-seed 1 --injector-seed 2 --out data/fixtures
sandboxbank seal data/fixtures [--expect <sha256>]
sandboxbank etl [--once]          (needs SANDBOXBANK_LEDGER_DSN and SANDBOXBANK_ANALYTICS_DSN)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

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


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "generate": _generate,
    "verify": _verify,
    "load-reference": _load_reference,
    "post": _post,
    "serve": _serve,
    "etl": _etl,
    "fixtures": _fixtures,
    "seal": _seal,
    "slice": _slice,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
