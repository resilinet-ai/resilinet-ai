"""``rai validate`` and the first runnable slice (B-14: FR-06.1; Build Plan section 5)."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from rai.cli.main import app
from sandboxbank.fixtures import build_slice

ROOT = Path(__file__).resolve().parents[2]
TXN = str(ROOT / "contracts" / "sandboxbank" / "transactions_raw.yaml")
runner = CliRunner()


def run(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["validate", *args])
    return result.exit_code, result.output


def refs(feed: Path) -> list[str]:
    return [
        "--ref",
        f"accounts={feed / 'accounts.csv'}",
        "--ref",
        f"merchants={feed / 'merchants.csv'}",
    ]


# ---- Build Plan section 5: acceptance for the first runnable slice --------------------


def test_first_runnable_slice(tmp_path: Path) -> None:
    """Clean batch exit 0; corrupted batch fails with exactly the 100 seeded rows, exit 1;
    running it twice gives byte-identical JSON."""
    build_slice(tmp_path, data_seed=7, injector_seed=8)  # 100,000 rows per batch
    feed = tmp_path / "feed"
    truth = [
        json.loads(line)
        for line in (tmp_path / "answer_key" / "ground_truth.jsonl").read_text("utf-8").splitlines()
    ]
    history = ["--history", str(tmp_path / "history.sqlite")]

    def batch(name: str) -> list[str]:
        return ["--contract", TXN, "--data", str(feed / "transactions_raw" / f"{name}.csv")]

    code, output = run(*batch("B000000"), *refs(feed), *history)
    assert code == 0, output
    assert output.startswith("PASS transactions_raw@1 B000000")

    code, first = run(*batch("B000001"), *refs(feed), *history, "--format", "json")
    assert code == 1
    report = json.loads(first)
    rules = {r["rule_id"]: r for r in report["rules"]}
    across = rules["unique_across_batches:txn_id"]
    seeded = truth[1]["rows"]
    assert len(seeded) == 100
    assert (across["outcome"], across["failing_count"]) == ("fail", 100)
    assert across["sample_rows"] == seeded[:20]
    assert [r["rule_id"] for r in report["verdict"]["blocking"]] == ["unique_across_batches:txn_id"]
    assert {r["outcome"] for r in report["rules"]} == {"pass", "fail"}

    code, second = run(*batch("B000001"), *refs(feed), *history, "--format", "json")
    assert code == 1
    assert second == first  # byte-identical: re-validating never compares a batch with itself


# ---- reports -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("slice")
    build_slice(
        out, data_seed=17, injector_seed=18, accounts=200, merchants=40, rows_per_batch=2_000
    )
    return out


def test_text_report_and_report_file(small: Path, tmp_path: Path) -> None:
    feed = small / "feed"
    report_file = tmp_path / "out" / "report.json"
    code, output = run(
        "--contract", TXN, "--data", str(feed / "transactions_raw" / "B000000.csv"), *refs(feed),
        "--history", str(tmp_path / "h.sqlite"), "--report", str(report_file), "--show-passing",
        "--as-of", "2026-01-01T00:15:00Z", "--batch-id", "first",
    )  # fmt: skip
    assert code == 0, output
    assert "PASS transactions_raw@1 first" in output
    assert "ok   amount.max_lookup (critical, threshold 0) 0/2,000 rows" in output
    saved = json.loads(report_file.read_text("utf-8"))
    assert saved["batch"]["id"] == "first"
    assert saved["as_of"] == "2026-01-01T00:15:00+00:00"
    assert saved["batch"]["source"]["sha256"] is not None
    assert saved["notes"] == []


def test_without_history_the_cross_batch_rule_cannot_run(small: Path) -> None:
    feed = small / "feed"
    code, output = run(
        "--contract", TXN, "--data", str(feed / "transactions_raw" / "B000000.csv"), *refs(feed)
    )
    assert code == 1
    assert "ERR  unique_across_batches:txn_id (critical, threshold 0)" in output
    assert "no batch history supplied for cross-batch duplicates" in output


def test_fail_on_and_parquet_input(small: Path, tmp_path: Path) -> None:
    contract = tmp_path / "c.yaml"
    contract.write_text(
        "contract: t\nversion: 1\ncolumns:\n  v: { type: string, allowed: [a], severity: warn }\n",
        encoding="utf-8",
    )
    data = tmp_path / "batch.parquet"
    pl.DataFrame({"v": ["a", "b"]}).write_parquet(data)
    assert run("--contract", str(contract), "--data", str(data))[0] == 0
    code, output = run("--contract", str(contract), "--data", str(data), "--fail-on", "warn")
    assert code == 1
    assert "FAIL t@1 batch - 1 blocking (v.allowed)" in output


def test_connector_mode(small: Path, tmp_path: Path) -> None:
    config = tmp_path / "files.yaml"
    config.write_text(
        f"name: feed\nenvironment: SANDBOX-01\ntype: csv\nroot: {(small / 'feed').as_posix()}\n",
        encoding="utf-8",
    )
    code, output = run(
        "--contract", TXN, "--connector", str(config), "--environment", "SANDBOX-01",
        "--asset", "transactions_raw/B000000.csv", "--ref", "accounts=accounts.csv",
        "--ref", "merchants=merchants.csv", "--history", str(tmp_path / "h.sqlite"),
        "--format", "json",
    )  # fmt: skip
    assert code == 0, output
    assert json.loads(output)["batch"]["source"] == {
        "type": "connector",
        "connector": "feed",
        "environment": "SANDBOX-01",
        "asset": "transactions_raw/B000000.csv",
    }
    code, output = run(
        "--contract",
        TXN,
        "--connector",
        str(config),
        "--environment",
        "PRODUCTION",
        "--asset",
        "x.csv",
    )
    assert code == 2
    assert "belongs to environment" in output


# ---- the command could not run: exit code 2 ---------------------------------------------


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "exactly one of --data FILE or --connector CONFIG"),
        (["--data", "missing.csv"], "data file missing.csv not found"),
        (["--data", "{csv}", "--ref", "accounts"], "must look like name=location"),
        (["--data", "{csv}", "--as-of", "yesterday"], "not an ISO 8601 timestamp"),
        (["--data", "{csv}", "--as-of", "2026-01-01T00:00:00"], "needs a time zone"),
        (["--data", "{csv}", "--fail-on", "fatal"], "--fail-on must be"),
        (["--data", "{csv}", "--format", "xml"], "--format must be"),
        (["--data", "{txt}"], "only .csv and .parquet"),
        (["--data", "{bad_parquet}"], "could not read"),
        (["--connector", "{csv}"], "needs --asset and --environment"),
        (
            ["--connector", "{list_yaml}", "--asset", "a", "--environment", "e"],
            "must be a YAML mapping",
        ),
        (
            ["--connector", "{missing_yaml}", "--asset", "a", "--environment", "e"],
            "could not read connector",
        ),
    ],
)
def test_usage_problems_exit_2(tmp_path: Path, extra: list[str], message: str) -> None:
    files = {
        "csv": tmp_path / "b.csv",
        "txt": tmp_path / "b.txt",
        "bad_parquet": tmp_path / "b.parquet",
        "list_yaml": tmp_path / "list.yaml",
        "missing_yaml": tmp_path / "nope.yaml",
    }
    files["csv"].write_text("v\n1\n", encoding="utf-8")
    files["txt"].write_text("v\n", encoding="utf-8")
    files["bad_parquet"].write_bytes(b"nope")
    files["list_yaml"].write_text("- a\n", encoding="utf-8")
    args = [a.format(**{k: str(v) for k, v in files.items()}) for a in extra]
    code, output = run("--contract", TXN, *args)
    assert code == 2, output
    assert message in output


def test_bad_contract_exits_2(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("contract: x\nversion: 1\ncolumns: {a: {type: nope}}\n", encoding="utf-8")
    data = tmp_path / "b.csv"
    data.write_text("a\n1\n", encoding="utf-8")
    code, output = run("--contract", str(bad), "--data", str(data))
    assert (code, "unknown type 'nope'" in output) == (2, True)
    code, output = run("--contract", str(tmp_path / "none.yaml"), "--data", str(data))
    assert (code, "could not read contract" in output) == (2, True)
