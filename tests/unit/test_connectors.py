"""Connectors and secret references, without a database (B-13: FR-02.1, FR-02.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from rai.connectors import ConnectorError, connector, parse_config
from rai.connectors.base import to_text
from rai.connectors.secrets import MASK, Secret, SecretError, SecretRef, scrub
from rai.validation.contract import parse_contract
from rai.validation.engine import validate
from rai.validation.verdict import decide

FAKE = "not-a-real-credential-7f3a"  # test value only


# ---- secret references ------------------------------------------------------------


def test_env_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAI_TEST_SECRET", FAKE)
    secret = SecretRef.parse("env:RAI_TEST_SECRET").resolve()
    assert secret.reveal() == FAKE
    assert FAKE not in repr(secret)
    assert FAKE not in str(secret)
    assert repr(secret) == f"Secret(env:RAI_TEST_SECRET, {MASK})"


def test_file_reference(tmp_path: Path) -> None:
    path = tmp_path / "db_password"
    path.write_text(FAKE + "\n", encoding="utf-8")
    ref = SecretRef.parse(f"file:{path.as_posix()}")  # /tmp/... on Linux, C:/... on Windows
    assert ref.resolve().reveal() == FAKE
    assert str(ref) == f"file:{path.as_posix()}"


@pytest.mark.parametrize(
    "text", ["plain-password", "env:", "env:1BAD", "file:relative/path", "vault:x"]
)
def test_malformed_references_are_rejected(text: str) -> None:
    with pytest.raises(SecretError, match="must be env:VARIABLE or file:/absolute/path"):
        SecretRef.parse(text)


def test_unresolvable_references_name_the_reference_not_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAI_MISSING_SECRET", raising=False)
    with pytest.raises(SecretError, match="RAI_MISSING_SECRET named by env:RAI_MISSING_SECRET"):
        SecretRef.parse("env:RAI_MISSING_SECRET").resolve()
    with pytest.raises(SecretError, match="does not exist"):
        SecretRef("file", "/nonexistent/rai/secret").resolve()


def test_scrub_removes_values_url_passwords_and_pairs() -> None:
    secret = Secret(FAKE, SecretRef("env", "X"))
    text = (
        f"failed for postgresql://ledger:{FAKE}@db:5432/x "
        f"and also password={FAKE} token='abc def' api_key=xyz value {FAKE}"
    )
    cleaned = scrub(text, secret)
    assert FAKE not in cleaned
    assert "postgresql://ledger:****@db" in cleaned
    assert "token=****" in cleaned
    assert "api_key=****" in cleaned
    assert scrub("nothing here", "") == "nothing here"


# ---- configs --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["password", "db_password", "token", "dsn", "API_KEY"])
def test_literal_credentials_are_refused_in_configs(key: str) -> None:
    config = {"name": "ledger", "environment": "SANDBOX-01", "type": "csv", "root": ".", key: FAKE}
    with pytest.raises(ConnectorError) as info:
        parse_config(config)
    assert "use secret_ref" in str(info.value)
    assert FAKE not in str(info.value)


def test_postgres_config_needs_a_valid_secret_reference() -> None:
    base = {
        "name": "ledger",
        "environment": "SANDBOX-01",
        "type": "postgres",
        "host": "localhost",
        "database": "ledger",
        "user": "ledger",
    }
    parsed = parse_config(base | {"secret_ref": "env:LEDGER_DB_PASSWORD"})
    assert (
        connector(base | {"secret_ref": "env:LEDGER_DB_PASSWORD"}).describe() == parsed.model_dump()
    )
    with pytest.raises(ConnectorError, match="secret_ref"):
        parse_config(base | {"secret_ref": FAKE})
    with pytest.raises(ConnectorError, match="type"):
        parse_config(base | {"type": "oracle", "secret_ref": "env:X"})


# ---- file connectors ---------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "sandbox"
    (root / "batches").mkdir(parents=True)
    (root / "batches" / "b1.csv").write_text("id,amount\n1,10.50\n2,\n", encoding="utf-8")
    (tmp_path / "outside.csv").write_text("id\n9\n", encoding="utf-8")
    return root


def files(root: Path, kind: str = "csv"):  # type: ignore[no-untyped-def]
    return connector(
        {"name": "files", "environment": "SANDBOX-01", "type": kind, "root": str(root)}
    )


def test_csv_is_read_as_text(data_root: Path) -> None:
    frame = files(data_root).read("batches/b1.csv", environment="SANDBOX-01")
    assert frame.schema == pl.Schema({"id": pl.Utf8, "amount": pl.Utf8})
    assert frame.to_dicts() == [{"id": "1", "amount": "10.50"}, {"id": "2", "amount": None}]
    assert files(data_root).read("batches/b1.csv", environment="SANDBOX-01", limit=1).height == 1


@pytest.mark.parametrize("asset", ["../outside.csv", "batches/../../outside.csv", "missing.csv"])
def test_reads_stay_inside_the_root(data_root: Path, asset: str) -> None:
    with pytest.raises(ConnectorError, match=r"outside the connector root|not found"):
        files(data_root).read(asset, environment="SANDBOX-01")


def test_absolute_paths_are_refused(data_root: Path) -> None:
    with pytest.raises(ConnectorError, match="relative to the connector root"):
        files(data_root).read(str(data_root / "batches" / "b1.csv"), environment="SANDBOX-01")


def test_connector_is_scoped_to_its_environment(data_root: Path) -> None:
    with pytest.raises(ConnectorError, match="belongs to environment 'SANDBOX-01', not 'STAGING'"):
        files(data_root).read("batches/b1.csv", environment="STAGING")
    with pytest.raises(ConnectorError, match="limit"):
        files(data_root).read("batches/b1.csv", environment="SANDBOX-01", limit=0)


def test_unreadable_file_is_a_connector_error(data_root: Path) -> None:
    (data_root / "broken.parquet").write_bytes(b"not parquet")
    with pytest.raises(ConnectorError, match=r"could not read 'broken\.parquet'"):
        files(data_root, "parquet").read("broken.parquet", environment="SANDBOX-01")


# ---- typed sources validate like text ------------------------------------------------

TYPED = parse_contract(
    """
contract: typed
version: 1
columns:
  id:      { type: integer, required: true }
  amount:  { type: "decimal(10,2)", min: 0 }
  ratio:   { type: float }
  ok:      { type: boolean }
  day:     { type: date }
  at:      { type: timestamp }
  label:   { type: string }
"""
)


def typed_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "id": [1, 2],
            "amount": pl.Series([Decimal("10.50"), None], dtype=pl.Decimal(10, 2)),
            "ratio": [0.25, 1.5],
            "ok": [True, None],
            "day": [date(2026, 1, 31), date(2026, 2, 1)],
            "at": pl.Series(
                [datetime(2026, 1, 1, 2, 0, 0, 10_000, tzinfo=UTC), None],
                dtype=pl.Datetime("us", "Europe/Berlin"),
            ),
            "label": ["x", None],
        }
    )


def test_to_text_produces_engine_forms() -> None:
    text = to_text(typed_frame())
    assert set(text.schema.values()) == {pl.Utf8}
    assert text.row(0, named=True) == {
        "id": "1",
        "amount": "10.50",
        "ratio": "0.25",
        "ok": "true",
        "day": "2026-01-31",
        "at": "2026-01-01T02:00:00.010000Z",
        "label": "x",
    }
    assert text.row(1, named=True)["ok"] is None
    assert text.row(1, named=True)["at"] is None
    naive = to_text(pl.DataFrame({"at": [datetime(2026, 1, 1, 0, 0)]}))
    assert naive.item() == "2026-01-01T00:00:00.000000"
    assert to_text(pl.DataFrame()).is_empty()


def test_parquet_source_validates_cleanly(tmp_path: Path) -> None:
    typed_frame().write_parquet(tmp_path / "typed.parquet")
    frame = files(tmp_path, "parquet").read("typed.parquet", environment="SANDBOX-01")
    verdict = decide(validate(TYPED, frame))
    assert verdict.passed, verdict.summary()
    assert verdict.warnings == ()


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("file:/run/secrets/ledger_password", "/run/secrets/ledger_password"),
        ("file:C:/Users/dev/secrets/pw", "C:/Users/dev/secrets/pw"),
        (r"file:C:\Users\dev\secrets\pw", r"C:\Users\dev\secrets\pw"),
    ],
)
def test_file_references_accept_absolute_posix_and_windows_paths(text: str, target: str) -> None:
    assert SecretRef.parse(text) == SecretRef("file", target)


@pytest.mark.parametrize("text", ["file:C:relative", "file:secrets/pw"])
def test_file_references_must_be_absolute(text: str) -> None:
    with pytest.raises(SecretError):
        SecretRef.parse(text)
