"""Command line for the SandboxBank generator.

sandboxbank generate --seed 42 --out data/sandboxbank --batches 3
sandboxbank verify data/sandboxbank
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sandboxbank.dataset import verify_dataset, write_dataset
from sandboxbank.generator import GeneratorConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandboxbank", description="SandboxBank synthetic data (synthetic only)."
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
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
            print(f"error: {error}", file=sys.stderr)
            return 2
        rows = sum(entry["rows"] for entry in manifest["files"])
        print(
            f"Wrote {len(manifest['files'])} files, {rows} rows, "
            f"seed {config.seed} ({config.purpose}) to {args.out}"
        )
        return 0

    problems = verify_dataset(args.out)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{args.out}: all files match the manifest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
