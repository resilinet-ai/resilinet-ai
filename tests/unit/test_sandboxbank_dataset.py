"""SandboxBank dataset files, manifest and CLI (B-02, NFR-REP-1)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sandboxbank.cli import main
from sandboxbank.dataset import verify_dataset, write_dataset
from sandboxbank.generator import GeneratorConfig
from sandboxbank.schema import column_names

GOLDEN = GeneratorConfig(seed=7, accounts=50, merchants=10, batches=2, rows_per_batch=200)

# Byte-for-byte fingerprints of GOLDEN. CI runs on Linux and development on Windows,
# so this test is what proves the data is identical across platforms. If a change
# alters them on purpose, bump SCHEMA_VERSION and record it in docs/decision-log.md.
GOLDEN_SHA256 = {
    "accounts.csv": "71b8b54bf9c05663e80ff53e98482f32e6083978d2734e0a3c5106f0accbe3e0",
    "merchants.csv": "5f4da5c1b75733f497e96bed081831c88c2075270d6bad7b7ec880361535ee3c",
    "transactions_raw/B000000.csv": (
        "bab0e3e5321b0f6734152ab0955f484f77ed08ec5a6da8fd0441b60b526225e8"
    ),
    "transactions_raw/B000001.csv": (
        "d7d62b6f1cd142d299bd8f1ceccd568333a6dd2f9aa943f2e15eaae6ebe2f2c7"
    ),
}


def test_golden_dataset_is_byte_identical(tmp_path: Path) -> None:
    manifest = write_dataset(GOLDEN, tmp_path)
    assert {f["path"]: f["sha256"] for f in manifest["files"]} == GOLDEN_SHA256


def test_two_runs_produce_identical_directories(tmp_path: Path) -> None:
    write_dataset(GOLDEN, tmp_path / "a")
    write_dataset(GOLDEN, tmp_path / "b")
    for name in [*GOLDEN_SHA256, "manifest.json"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_files_are_csv_with_header_and_lf_endings(tmp_path: Path) -> None:
    manifest = write_dataset(GOLDEN, tmp_path)
    for entry in manifest["files"]:
        raw = (tmp_path / entry["path"]).read_bytes()
        assert b"\r" not in raw
        with (tmp_path / entry["path"]).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert tuple(rows[0]) == column_names(entry["table"])
        assert len(rows) - 1 == entry["rows"]


def test_manifest_records_seed_purpose_and_no_wall_clock(tmp_path: Path) -> None:
    write_dataset(GOLDEN, tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 7
    assert manifest["seed_purpose"] == "development"
    assert manifest["synthetic"] is True
    assert manifest["schema_version"] == "1"
    assert "generated_at" not in manifest


def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    write_dataset(GOLDEN, tmp_path)
    with pytest.raises(FileExistsError):
        write_dataset(GOLDEN, tmp_path)
    write_dataset(GOLDEN, tmp_path, force=True)


def test_verify_detects_tampering_and_missing_files(tmp_path: Path) -> None:
    write_dataset(GOLDEN, tmp_path)
    assert verify_dataset(tmp_path) == []

    with (tmp_path / "accounts.csv").open("a", encoding="utf-8", newline="") as handle:
        handle.write("ACC-9999999,CUS-9999999,checking,USD,active,2020-01-01,5000.00\n")
    (tmp_path / "merchants.csv").unlink()
    problems = verify_dataset(tmp_path)
    assert "accounts.csv: SHA-256 does not match the manifest" in problems
    assert "merchants.csv: missing" in problems


def test_verify_without_manifest(tmp_path: Path) -> None:
    assert verify_dataset(tmp_path) == [f"manifest.json not found in {tmp_path}"]


def test_cli_generate_then_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "data"
    args = ["--seed", "7", "--out", str(out), "--accounts", "50", "--merchants", "10"]
    assert main(["generate", *args, "--batches", "2", "--rows", "200"]) == 0
    assert "seed 7 (development)" in capsys.readouterr().out
    assert main(["verify", str(out)]) == 0
    assert "all files match" in capsys.readouterr().out

    (out / "transactions_raw" / "B000001.csv").write_text("tampered\n", encoding="utf-8")
    assert main(["verify", str(out)]) == 1
    assert "B000001.csv" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [["--seed", "250000"], ["--seed", "1", "--rows", "0"]],
)
def test_cli_rejects_bad_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    assert main(["generate", "--out", str(tmp_path / "x"), *extra]) == 2
    assert capsys.readouterr().err.startswith("error:")
