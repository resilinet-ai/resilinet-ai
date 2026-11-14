"""First runnable slice demo (Build Plan section 5), for the 2-minute screen recording.

    python -m uv run python scripts/demo_slice.py        (or: make demo)

1. SandboxBank generates two batches of 100,000 transactions.
2. The defect injector re-sends 100 rows of batch 1 inside batch 2 (D02, 0.1%).
3. rai validate passes batch 1 (exit 0) and fails batch 2 with exactly those
   100 rows (exit 1).
4. Validating batch 2 again gives byte-identical JSON.

Synthetic data only. Output goes to data/slice/ (git-ignored).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "slice"
FEED = OUT / "feed"
CONTRACT = ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml"


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        sys.exit(f"{name} not found: run this with 'python -m uv run python {__file__}'")
    return found


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def show(command: list[str]) -> subprocess.CompletedProcess[str]:
    shown = " ".join(Path(c).stem.lower() if i == 0 else c for i, c in enumerate(command))
    print(f"$ {shown.replace(str(ROOT) + '/', '').replace(str(ROOT), '.')}", flush=True)
    # Runs only our own rai and sandboxbank commands with fixed arguments.
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)  # noqa: S603


def validate(batch: str, *extra: str) -> list[str]:
    return [
        tool("rai"), "validate",
        "--contract", CONTRACT.relative_to(ROOT).as_posix(),
        "--data", (FEED / "transactions_raw" / f"{batch}.csv").relative_to(ROOT).as_posix(),
        "--ref", f"accounts={(FEED / 'accounts.csv').relative_to(ROOT).as_posix()}",
        "--ref", f"merchants={(FEED / 'merchants.csv').relative_to(ROOT).as_posix()}",
        "--history", (OUT / "history.sqlite").relative_to(ROOT).as_posix(),
        *extra,
    ]  # fmt: skip


def main() -> int:
    step("1-2. Generate two batches and seed 100 re-sent rows into the second")
    made = show([tool("sandboxbank"), "slice", "--out", "data/slice", "--force"])
    print(made.stdout or made.stderr, end="")
    if made.returncode:
        return made.returncode
    truth = [json.loads(line) for line in (OUT / "answer_key" / "ground_truth.jsonl").open()]

    step("3a. Validate the clean batch")
    first = show(validate("B000000"))
    print(first.stdout, end="")
    print(f"exit code: {first.returncode}")

    step("3b. Validate the batch with re-sent rows")
    second = show(validate("B000001"))
    print(second.stdout, end="")
    print(f"exit code: {second.returncode}")

    step("4. Repeatability: validate batch 2 twice more as JSON and compare bytes")
    runs = [show(validate("B000001", "--format", "json")).stdout for _ in range(2)]
    report = json.loads(runs[0])
    across = next(r for r in report["rules"] if r["rule_id"] == "unique_across_batches:txn_id")
    seeded = truth[1]["rows"]
    checks = {
        "clean batch exits 0": first.returncode == 0,
        "corrupted batch exits 1": second.returncode == 1,
        f"exactly {len(seeded)} seeded rows reported": across["failing_count"]
        == len(seeded)
        == 100,
        "reported rows match the answer key": across["sample_rows"] == seeded[:20],
        "JSON byte-identical across runs": runs[0] == runs[1],
    }
    step("Result")
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
