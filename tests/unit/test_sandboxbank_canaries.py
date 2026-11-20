"""Canary customers for the privacy sentinel (FR-05.1): realistic but impossible values,
unique needles, a deterministic stream that leaves every existing file unchanged."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sandboxbank import canaries, cli
from sandboxbank.canaries import generate_canaries, luhn_check_digit, luhn_valid
from sandboxbank.dataset import write_dataset
from sandboxbank.generator import GeneratorConfig

CUSTOMERS = [f"CUS-{n:07d}" for n in range(1, 201)]


def test_luhn_against_known_numbers() -> None:
    assert luhn_valid("4111111111111111")  # the classic test card
    assert luhn_valid("79927398713")  # the textbook example
    assert not luhn_valid("4111111111111112")
    assert luhn_check_digit("7992739871") == "3"


def test_values_look_real_but_belong_to_no_one() -> None:
    made = generate_canaries(7, CUSTOMERS, 20)
    for c in made:
        assert c.customer_id in CUSTOMERS
        assert re.fullmatch(r"cn[0-9a-f]{10}", c.marker)
        assert c.email.endswith(f".{c.marker}@example.invalid")  # reserved, never resolves
        assert re.fullmatch(r"\+1 \d{3} 555 01\d{2}", c.phone)  # the fictional range
        assert c.card_number.startswith(canaries.TEST_BIN)
        assert len(c.card_number) == 16
        assert luhn_valid(c.card_number)  # card detectors must still match it
        assert re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", c.full_name)
    assert len({c.customer_id for c in made}) == 20  # one canary per customer


def test_every_needle_is_unique_across_canaries() -> None:
    needles = [v for c in generate_canaries(3, CUSTOMERS, 50) for v in c.needles().values()]
    assert len(needles) == len(set(needles))


def test_needles_cover_the_written_forms_a_leak_may_take() -> None:
    [c] = generate_canaries(11, CUSTOMERS, 1)
    needles = c.needles()
    assert needles["card_number_spaced"].replace(" ", "") == c.card_number
    assert needles["phone_digits"] == "".join(ch for ch in c.phone if ch.isdigit())
    assert "date_of_birth" not in needles  # too common to search for


def test_same_seed_same_canaries_other_seed_others() -> None:
    assert generate_canaries(5, CUSTOMERS) == generate_canaries(5, CUSTOMERS)
    assert generate_canaries(5, CUSTOMERS) != generate_canaries(6, CUSTOMERS)


@pytest.mark.parametrize(
    ("count", "message"), [(0, "count must be 1 to 50"), (51, "count must be 1 to 50"),
                           (3, "at least 3 customers")],
)  # fmt: skip
def test_impossible_counts_are_refused(count: int, message: str) -> None:
    customers = CUSTOMERS if count != 3 else ["CUS-1", "CUS-2", "CUS-1"]
    with pytest.raises(ValueError, match=message):
        generate_canaries(1, customers, count)


def test_writing_canaries_leaves_the_dataset_byte_identical(tmp_path: Path) -> None:
    config = GeneratorConfig(seed=9, accounts=300, merchants=20, batches=1, rows_per_batch=50)
    before = write_dataset(config, tmp_path / "data")
    assert cli.main(["canaries", "write", "--accounts", str(tmp_path / "data" / "accounts.csv"),
                     "--seed", "9", "--out", str(tmp_path / "canaries")]) == 0  # fmt: skip
    assert write_dataset(config, tmp_path / "again") == before  # its own stream: no change

    key = json.loads((tmp_path / "canaries" / canaries.KEY_FILE).read_text(encoding="utf-8"))
    assert key["format"] == canaries.FORMAT
    assert len(key["canaries"]) == 5
    written = canaries.read_customers(tmp_path / "canaries" / canaries.CUSTOMERS_FILE)
    assert [c.marker for c in written] == [k["marker"] for k in key["canaries"]]
    assert written[0].needles() == key["canaries"][0]["needles"]


def test_the_cli_reports_a_missing_accounts_file(tmp_path: Path) -> None:
    missing = cli.main(["canaries", "write", "--accounts", str(tmp_path / "nope.csv"),
                        "--seed", "1", "--out", str(tmp_path / "out")])  # fmt: skip
    assert missing == 2


def test_the_etl_never_copies_customers() -> None:
    """A correct pipeline leaks nothing: any canary downstream is a real leak."""
    from sandboxbank import etl

    assert "customers" not in {source for _, source, _ in etl._DIMENSIONS}
    warehouse = (Path(etl.__file__).parent / "warehouse.sql").read_text(encoding="utf-8")
    assert not re.search(r"full_name|email|card_number|phone|date_of_birth", warehouse)
