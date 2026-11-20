"""Canary sweeps of logs and evidence bundles (FR-05.1, part 3): a value is found in
whatever file it hides, every hit says where, and nothing prints it in full."""

from __future__ import annotations

import gzip
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from rai.cli.main import app
from rai.core import evidence
from rai.privacy.canary import Needle, mask
from rai.privacy.sweep import load_key, sweep
from sandboxbank.canaries import KEY_FILE, NEEDLES_FILE, generate_canaries, write_canaries

runner = CliRunner()


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("canaries")
    write_canaries(generate_canaries(31, [f"CUS-{n:07d}" for n in range(1, 60)], 4), 31, out)
    return out


@pytest.fixture(scope="module")
def found(keys: Path) -> list[Needle]:
    needles, problem = load_key(keys / NEEDLES_FILE)
    assert problem is None
    return needles


def value(found: list[Needle], kind: str, n: int = 0) -> Needle:
    return [x for x in found if x.kind == kind][n]


def test_both_key_files_give_the_same_needles(keys: Path, found: list[Needle]) -> None:
    from_json, problem = load_key(keys / KEY_FILE)
    assert problem is None
    assert set(from_json) == set(found)
    assert len(found) == 4 * 7


def test_clean_logs_and_bundles_pass(tmp_path: Path, found: list[Needle]) -> None:
    log = tmp_path / "worker.log"
    log.write_text('{"level":"info","message":"run started","run_id":"r-1"}\n', encoding="utf-8")
    bundle = evidence.build(uuid4(), {"steps.json": [{"error": None, "state": "done"}]})
    (tmp_path / "bundle.zip").write_bytes(bundle.data)
    result = sweep([str(tmp_path)], found)
    assert (result.outcome, result.hits, result.sources) == ("pass", 0, 2)
    assert "no canary value" in result.summary()


def test_a_leak_is_found_in_every_kind_of_file(tmp_path: Path, found: list[Needle]) -> None:
    email, card, phone = value(found, "email"), value(found, "card_number"), value(found, "phone")
    (tmp_path / "api.log").write_text(
        f"ok\nok\nlogin failed for {email.value.upper()}\n", encoding="utf-8"
    )
    (tmp_path / "old.log.gz").write_bytes(gzip.compress(f"call {phone.value}\n".encode()))
    (tmp_path / "export.json").write_text(
        json.dumps({"rows": [{"id": 1}, {"card": int(card.value)}]}), encoding="utf-8"
    )
    run = uuid4()
    leaky = evidence.build(run, {"steps.json": [{"error": f"bad row: {email.value}"}]})
    (tmp_path / "bundle.zip").write_bytes(leaky.data)

    result = sweep([str(tmp_path)], found)
    assert result.outcome == "fail"
    assert result.hits == 4
    assert dict(result.by_kind) == {"email": 2, "card_number": 1, "phone": 1}
    places = {(Path(f.source.split("!")[0]).name, f.place, f.kind) for f in result.sample}
    assert places == {
        ("api.log", "line 3", "email"),
        ("old.log.gz", "line 1", "phone"),
        ("export.json", "$.rows[1].card", "card_number"),
        ("bundle.zip", "$[0].error", "email"),
    }
    in_bundle = next(f for f in result.sample if "!" in f.source)
    assert in_bundle.source.endswith(f"!rai-evidence-{run}/steps.json")
    assert in_bundle.masked == mask(email.value)


def test_overlapping_values_count_once_as_the_longest(tmp_path: Path, found: list[Needle]) -> None:
    email = value(found, "email")  # it contains the canary's marker
    (tmp_path / "a.log").write_text(f"to {email.value}\n", encoding="utf-8")
    result = sweep([str(tmp_path)], found)
    assert (result.hits, dict(result.by_kind)) == (1, {"email": 1})


def test_standard_input_is_read_as_lines(found: list[Needle]) -> None:
    import io

    marker = value(found, "marker", 2)
    result = sweep(["-"], found, stdin=io.StringIO(f"etl-job | ok\nledger | {marker.value}\n"))
    assert [(f.source, f.place, f.kind) for f in result.sample] == [("<stdin>", "line 2", "marker")]


def test_case_sensitive_search_misses_a_changed_case(tmp_path: Path, found: list[Needle]) -> None:
    (tmp_path / "a.log").write_text(value(found, "email").value.upper(), encoding="utf-8")
    assert sweep([str(tmp_path)], found, case_sensitive=True).hits == 0


def test_an_unreadable_source_is_an_error_not_a_pass(tmp_path: Path, found: list[Needle]) -> None:
    (tmp_path / "broken.zip").write_bytes(b"not a zip")
    result = sweep([str(tmp_path), str(tmp_path / "missing.log")], found)
    assert result.outcome == "error"
    broken, missing = result.unreadable
    assert broken.startswith(f"{(tmp_path / 'broken.zip').as_posix()}: BadZipFile")
    assert missing == f"{tmp_path / 'missing.log'}: not found"


def test_the_key_itself_is_skipped(keys: Path, found: list[Needle]) -> None:
    result = sweep([str(keys / NEEDLES_FILE)], found, key_path=keys / NEEDLES_FILE)
    assert (result.outcome, result.sources, len(result.skipped)) == ("pass", 0, 1)


def test_the_command_masks_every_value_and_exits_1(tmp_path: Path, keys: Path,
                                                    found: list[Needle]) -> None:  # fmt: skip
    every = [n.value for n in found]
    (tmp_path / "dump.log").write_text("\n".join(every) + "\n", encoding="utf-8")
    for extra in ([], ["--json"]):
        out = runner.invoke(app, ["canary", "scan", "--key", str(keys / NEEDLES_FILE),
                                  str(tmp_path), *extra])  # fmt: skip
        assert out.exit_code == 1, out.output
        assert [v for v in every if v.lower() in out.output.lower()] == []
    report = json.loads(out.output)
    assert report["outcome"] == "fail"
    assert report["hits"] == len(every)


def test_the_command_exits_0_when_clean_and_2_on_a_bad_key(tmp_path: Path, keys: Path) -> None:
    (tmp_path / "a.log").write_text("nothing here\n", encoding="utf-8")
    clean = runner.invoke(app, ["canary", "scan", "--key", str(keys / NEEDLES_FILE), str(tmp_path)])
    assert clean.exit_code == 0, clean.output
    assert clean.output.startswith("CLEAN")
    bad = tmp_path / "bad.csv"
    bad.write_text("canary,kind,value\nc,k,abc\n", encoding="utf-8")
    refused = runner.invoke(app, ["canary", "scan", "--key", str(bad), str(tmp_path / "a.log")])
    assert refused.exit_code == 2
    assert "shorter than 6" in refused.output


def test_a_bundle_with_a_masked_leak_report_is_clean(found: list[Needle], tmp_path: Path) -> None:
    """What RAI writes about a leak (the masked observation) is not itself a leak."""
    import polars as pl

    from rai.privacy.canary import CanaryDefinition, scan
    from rai.worker.executor import _canary_observation

    definition = CanaryDefinition.model_validate({
        "connector": "w", "environment": "SANDBOX-01",
        "key": {"asset": "k", "connector": "f"}})  # fmt: skip
    target = pl.DataFrame({"notes": [n.value for n in found]})
    observation = _canary_observation(scan(target, found, definition), definition)
    bundle = evidence.build(uuid4(), {"observations.json": [observation]})
    (tmp_path / "b.zip").write_bytes(bundle.data)
    with zipfile.ZipFile(tmp_path / "b.zip") as archive:
        assert len(archive.namelist()) == 2  # the member and its manifest
    assert sweep([str(tmp_path)], found).outcome == "pass"
