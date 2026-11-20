"""The canary privacy sentinel (FR-05.1): needles are found wherever they hide, every
hit is masked, and a key that cannot work is an error."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from rai.privacy.canary import CanaryDefinition, mask, needles, scan
from rai.worker.executor import _canary_observation
from sandboxbank.canaries import NEEDLES_FILE, generate_canaries, write_canaries

BASE: dict[str, Any] = {"connector": "warehouse", "environment": "SANDBOX-01",
                        "key": {"asset": "canary-key", "connector": "feed"}}  # fmt: skip


def definition(**extra: Any) -> CanaryDefinition:
    return CanaryDefinition.model_validate(BASE | extra)


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory) -> pl.DataFrame:
    """The real thing: SandboxBank's needles.csv, read as a connector reads it."""
    out = tmp_path_factory.mktemp("canaries")
    write_canaries(generate_canaries(17, [f"CUS-{n:07d}" for n in range(1, 50)], 3), 17, out)
    return pl.read_csv(out / NEEDLES_FILE, infer_schema=False)


def value(key: pl.DataFrame, kind: str, n: int = 0) -> str:
    return str(key.filter(pl.col("kind") == kind)["value"][n])


def canary_of(key: pl.DataFrame, kind: str, n: int = 0) -> str:
    return str(key.filter(pl.col("kind") == kind)["canary"][n])


def test_the_sandboxbank_key_is_usable(key: pl.DataFrame) -> None:
    found, problem = needles(key)
    assert problem is None
    assert len(found) == 3 * 7  # seven needles per canary
    assert {n.kind for n in found} >= {"email", "card_number", "card_number_spaced", "marker"}


def test_a_clean_target_passes(key: pl.DataFrame) -> None:
    target = pl.DataFrame({"account_id": ["ACC-1", "ACC-2"], "notes": ["ok", "fine"]})
    result = scan(target, needles(key)[0], definition())
    assert (result.outcome, result.hits, result.rows) == ("pass", 0, 2)
    assert result.columns == ("account_id", "notes")
    assert "no canary value in 2 rows" in result.summary()


def test_leaks_are_found_inside_text_and_in_other_case(key: pl.DataFrame) -> None:
    email = value(key, "email")
    spaced = value(key, "card_number_spaced", 1)
    target = pl.DataFrame({
        "id": ["1", "2", "3"],
        "notes": ["fine", f"contact {email.upper()} please", "fine"],
        "card": ["", "", f"paid with {spaced}"],
    })  # fmt: skip
    result = scan(target, needles(key)[0], definition())
    assert result.outcome == "fail"
    assert (result.hits, result.hit_rows) == (2, 2)
    assert result.by_kind == {"email": 1, "card_number_spaced": 1}
    assert result.by_column == {"notes": 1, "card": 1}
    first = result.sample[0]
    assert (first.column, first.row, first.kind) == ("notes", 1, "email")
    assert first.canary == canary_of(key, "email")
    assert first.masked == mask(email)


def test_no_needle_is_ever_repeated_in_full(key: pl.DataFrame) -> None:
    every = [str(v) for v in key["value"]]
    target = pl.DataFrame({"dump": [" | ".join(every)]})  # every needle leaked at once
    result = scan(target, needles(key)[0], definition())
    assert result.hits >= len(every)
    written = json.dumps([asdict(h) for h in result.sample]) + result.summary()
    written += json.dumps(_canary_observation(result, definition()))
    leaked = [v for v in every if v.lower() in written.lower()]
    assert leaked == []


def test_case_sensitive_search_misses_a_changed_case(key: pl.DataFrame) -> None:
    target = pl.DataFrame({"notes": [value(key, "email").upper()]})
    assert scan(target, needles(key)[0], definition(case_sensitive=True)).hits == 0


def test_only_the_listed_columns_are_searched(key: pl.DataFrame) -> None:
    target = pl.DataFrame({"a": [value(key, "marker")], "b": ["x"]})
    assert scan(target, needles(key)[0], definition(columns=["b"])).hits == 0
    missing = scan(target, needles(key)[0], definition(columns=["b", "nope"]))
    assert missing.outcome == "error"
    assert missing.error == "column(s) 'nope' not in the target"


def test_masking_keeps_only_the_ends() -> None:
    assert mask("avery.barcor.cn0123456789@example.invalid") == "av…id, 41 chars"
    assert mask("short") == "•••, 5 chars"


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pl.DataFrame({"canary": ["c"], "value": ["abcdefgh"]}), "no 'kind' column"),
        (pl.DataFrame({"canary": [], "kind": [], "value": []}, schema=dict.fromkeys(
            ("canary", "kind", "value"), pl.String)), "lists no needles"),
        (pl.DataFrame({"canary": ["c"], "kind": ["k"], "value": ["abc"]}),
         "shorter than 6 characters"),
        (pl.DataFrame({"canary": ["c", "d"], "kind": ["k", "k"],
                       "value": ["Secret-1", "secret-1"]}), "listed twice"),
    ],
)  # fmt: skip
def test_a_key_that_cannot_work_is_refused(frame: pl.DataFrame, message: str) -> None:
    found, problem = needles(frame)
    assert found == []
    assert problem is not None
    assert message in problem


def test_a_bad_definition_is_refused() -> None:
    with pytest.raises(ValidationError, match="key"):
        CanaryDefinition.model_validate({k: v for k, v in BASE.items() if k != "key"})
    with pytest.raises(ValidationError, match="listed twice"):
        definition(columns=["a", "a"])


def test_needles_csv_matches_the_json_key(tmp_path: Path) -> None:
    made = generate_canaries(4, [f"CUS-{n:07d}" for n in range(1, 10)], 2)
    answer = write_canaries(made, 4, tmp_path)
    table = pl.read_csv(tmp_path / NEEDLES_FILE, infer_schema=False)
    from_json = {(c["customer_id"], k, v) for c in answer["canaries"]
                 for k, v in c["needles"].items()}  # fmt: skip
    assert set(table.iter_rows()) == from_json
