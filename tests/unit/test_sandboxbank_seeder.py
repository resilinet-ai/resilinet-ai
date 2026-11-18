"""The seeding log: every acknowledged write, hash-chained, outside the ledger (B-30; T-07)."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sandboxbank.cli import main
from sandboxbank.seeder import FIELDS, AckLog, seed, verify

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def rows(n: int) -> list[dict[str, str]]:
    return [
        {"txn_id": f"00000000-0000-4000-8000-{i:012}", "batch_id": "B000000",
         "account_id": "A1", "merchant_id": "", "channel": "ach", "direction": "debit",
         "amount": "1.00", "currency": "USD", "status": "posted",
         "created_at": "2026-11-17T12:00:00Z", "is_fraud": "false"}
        for i in range(n)
    ]  # fmt: skip


class Api:
    """Answers in turn: (status, body); records what it was sent."""

    def __init__(self, *answers: tuple[int, dict[str, Any] | None]) -> None:
        self.answers = list(answers)
        self.sent: list[tuple[str, str]] = []

    def __call__(self, url: str, body: dict[str, Any], timeout: float) -> Any:
        self.sent.append((url, body["txn_id"]))
        return self.answers.pop(0)


def clock() -> Any:
    ticks = iter(T0 + timedelta(seconds=n) for n in range(1000))
    return lambda: next(ticks)


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_every_attempt_is_logged_and_only_2xx_answers_are_acknowledged(tmp_path: Path) -> None:
    log = AckLog(tmp_path / "acks.csv")
    api = Api((201, {"seq": 7}), (200, {"seq": 7}), (503, None), (0, None), (422, {"seq": None}))
    waits: list[float] = []
    totals = seed("http://api", rows(5), log, rate=1000, post=api, clock=clock(),
                  sleep=waits.append)  # fmt: skip
    assert totals == {"created": 1, "replayed": 1, "unacknowledged": 2, "rejected": 1}
    assert api.sent[0][0] == "http://api/v1/transactions"
    logged = read(log.path)
    assert [(r["outcome"], r["acked"], r["seq"]) for r in logged] == [
        ("created", "true", "7"), ("replayed", "true", "7"), ("unacknowledged", "false", ""),
        ("unacknowledged", "false", ""), ("rejected", "false", ""),
    ]  # fmt: skip
    assert logged[0]["acked_at"] == "2026-11-17T12:00:00.000000Z"
    assert verify(log.path) == (5, log.head)


def test_a_restarted_seeder_continues_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "acks.csv"
    seed("http://api", rows(2), AckLog(path), rate=1000, post=Api((201, {"seq": 1}),
         (201, {"seq": 2})), clock=clock(), sleep=lambda _: None)  # fmt: skip
    again = AckLog(path)
    assert again.count == 2
    seed("http://api", rows(3)[2:], again, rate=1000, post=Api((201, {"seq": 3})),
         clock=clock(), sleep=lambda _: None)  # fmt: skip
    assert [r["n"] for r in read(path)] == ["1", "2", "3"]
    assert verify(path)[0] == 3


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (lambda lines: [*lines[:2], *lines[3:]], "missing, reordered or malformed"),
        (lambda lines: [*lines[:2], lines[2].replace(",true,", ",false,"), *lines[3:]],
         "hash chain is broken"),
        (lambda lines: ["n,txn_id\n", *lines[1:]], "not a seeding log"),
    ],
)  # fmt: skip
def test_an_edited_or_truncated_log_is_detected(tmp_path: Path, tamper: Any, message: str) -> None:
    path = tmp_path / "acks.csv"
    seed("http://api", rows(3), AckLog(path), rate=1000,
         post=Api(*[(201, {"seq": n}) for n in range(3)]), clock=clock(),
         sleep=lambda _: None)  # fmt: skip
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(tamper(lines)), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        verify(path)
    assert main(["verify-acks", str(path)]) == 1


def test_the_seeder_paces_itself_and_checks_its_arguments(tmp_path: Path) -> None:
    waits: list[float] = []
    seed("http://api", rows(2), AckLog(tmp_path / "a.csv"), rate=2,
         post=Api((201, {"seq": 1}), (201, {"seq": 2})), clock=clock(),
         sleep=waits.append)  # fmt: skip
    assert len(waits) == 2
    assert all(0.4 < w <= 0.5 for w in waits)
    with pytest.raises(ValueError, match="http or https"):
        seed("file:///etc", rows(1), AckLog(tmp_path / "b.csv"))
    with pytest.raises(ValueError, match="rate must be"):
        seed("http://api", rows(1), AckLog(tmp_path / "b.csv"), rate=0)


def test_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "acks.csv"
    AckLog(path)
    assert main(["verify-acks", str(path)]) == 0
    assert "intact: 0 rows" in capsys.readouterr().out
    assert main(["seed", "--data", str(tmp_path / "none"), "--log", str(path)]) == 2
    assert path.read_text(encoding="utf-8").splitlines() == [",".join(FIELDS)]  # nothing sent
