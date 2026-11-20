"""Synthetic canary customers for RAI's privacy sentinel (FR-05.1).

SandboxBank's feed carries no personal data: accounts name a ``customer_id`` only.
A canary is a fake customer with realistic-looking personal details, linked to a real
``customer_id`` of the dataset. Canaries are loaded into ledger-db's ``customers``
table, which nothing downstream is meant to copy. So a canary value found anywhere
else (the warehouse, a log, an evidence bundle) is a leak, and an exact one: every
value is unique, so a match is never a coincidence.

Each canary's values look real to redaction code but cannot belong to anyone:

* **email:** ``<first>.<last>.<marker>@example.invalid``. The ``.invalid`` top-level
  domain is reserved and can never resolve (RFC 2606).
* **phone:** ``+1 <area> 555 01xx``, the North American range reserved for fiction.
* **card number:** 16 digits in the ``400000`` test range with a valid Luhn check
  digit, so card-number detectors match it, and no real card has it.
* **name:** a common first name and a surname built from syllables, never a list of
  real people.
* **date of birth:** a plain date; too common to search for, so not a needle.

The ``marker`` (``cn`` and 10 hex digits) ties every value to its canary. The
*needles*, the values RAI searches for, are the marker, the email, the card number in
both written forms, the phone in both written forms and the name.

Canaries come from their own random stream of the seed, so writing them changes no
existing file: a dataset and its fixtures stay byte-identical.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sandboxbank.generator import stream

FORMAT = "sandboxbank-canaries/1"
CUSTOMERS_FILE = "customers.csv"
KEY_FILE = "canaries.json"
NEEDLES_FILE = "needles.csv"  # the same key as a table: canary, kind, value
COLUMNS = ("customer_id", "full_name", "email", "phone", "card_number", "date_of_birth",
           "marker")  # fmt: skip
MAX_CANARIES = 50
TEST_BIN = "400000"

FIRST_NAMES = ("Avery", "Blake", "Casey", "Dana", "Elliot", "Finley", "Harper", "Jordan",
               "Kendall", "Logan", "Morgan", "Parker", "Quinn", "Reese", "Rowan", "Sawyer",
               "Skyler", "Taylor")  # fmt: skip
SYLLABLES = ("bar", "cor", "dal", "fen", "gar", "hol", "kes", "lin", "mor", "nor", "pel",
             "quil", "ros", "tam", "vel", "wyn", "xan", "zor")  # fmt: skip
AREA_CODES = ("212", "303", "312", "415", "503", "602", "617", "702", "713", "808", "917")


@dataclass(frozen=True, slots=True)
class Canary:
    customer_id: str
    full_name: str
    email: str
    phone: str
    card_number: str
    date_of_birth: date
    marker: str

    def as_row(self) -> tuple[str, ...]:
        return (self.customer_id, self.full_name, self.email, self.phone, self.card_number,
                self.date_of_birth.isoformat(), self.marker)  # fmt: skip

    def needles(self) -> dict[str, str]:
        """What RAI searches for, by kind. Written forms a leak may take are included:
        a card number with or without spaces, a phone with or without punctuation."""
        digits = "".join(c for c in self.phone if c.isdigit())
        card = self.card_number
        return {
            "marker": self.marker,
            "email": self.email,
            "full_name": self.full_name,
            "card_number": card,
            "card_number_spaced": " ".join(card[i : i + 4] for i in range(0, 16, 4)),
            "phone": self.phone,
            "phone_digits": digits,
        }


def luhn_check_digit(partial: str) -> str:
    """The digit that makes ``partial`` + digit pass the Luhn check."""
    total = 0
    for position, char in enumerate(reversed(partial)):
        value = int(char)
        if position % 2 == 0:  # doubled: these are odd positions once the check digit is added
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def luhn_valid(number: str) -> bool:
    return number.isdigit() and luhn_check_digit(number[:-1]) == number[-1]


def generate_canaries(seed: int, customer_ids: list[str], count: int = 5) -> list[Canary]:
    """``count`` canaries from the seed's ``canaries`` stream, each linked to a distinct
    customer of the dataset. The same seed and customers give the same canaries."""
    unique = sorted(set(customer_ids))
    if not 1 <= count <= MAX_CANARIES:
        raise ValueError(f"count must be 1 to {MAX_CANARIES}")
    if count > len(unique):
        raise ValueError(f"{count} canaries need at least {count} customers; "
                         f"the dataset has {len(unique)}")  # fmt: skip
    rng = stream(seed, "canaries")
    chosen = sorted(rng.sample(unique, count))
    canaries = []
    seen: set[str] = set()
    for customer_id in chosen:
        while True:
            marker = f"cn{rng.getrandbits(40):010x}"
            first = rng.choice(FIRST_NAMES)
            last = "".join(rng.choice(SYLLABLES) for _ in range(3)).capitalize()
            partial = TEST_BIN + f"{rng.randrange(10**9):09d}"
            card = partial + luhn_check_digit(partial)
            phone = f"+1 {rng.choice(AREA_CODES)} 555 01{rng.randrange(100):02d}"
            values = {marker, f"{first} {last}", card, phone}
            if not values & seen:  # every needle unique across the canaries
                break
        seen |= values
        canaries.append(
            Canary(
                customer_id=customer_id,
                full_name=f"{first} {last}",
                email=f"{first.lower()}.{last.lower()}.{marker}@example.invalid",
                phone=phone,
                card_number=card,
                date_of_birth=date(1950, 1, 1) + timedelta(days=rng.randrange(365 * 50)),
                marker=marker,
            )
        )
    return canaries


def key(canaries: list[Canary], seed: int) -> dict[str, Any]:
    """The answer key RAI's sentinel reads: every needle, by canary."""
    return {
        "format": FORMAT,
        "seed": seed,
        "canaries": [
            {"customer_id": c.customer_id, "marker": c.marker, "needles": c.needles()}
            for c in canaries
        ],
    }


def write_canaries(canaries: list[Canary], seed: int, out_dir: Path) -> dict[str, Any]:
    """Write ``customers.csv`` and the key ``canaries.json``; return the key."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / CUSTOMERS_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(c.as_row() for c in canaries)
    with (out_dir / NEEDLES_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("canary", "kind", "value"))
        for c in canaries:
            writer.writerows((c.customer_id, kind, value) for kind, value in c.needles().items())
    answer = key(canaries, seed)
    answer["customers_sha256"] = hashlib.sha256((out_dir / CUSTOMERS_FILE).read_bytes()).hexdigest()
    (out_dir / KEY_FILE).write_text(json.dumps(answer, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8", newline="\n")  # fmt: skip
    return answer


def read_customers(path: Path) -> list[Canary]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            Canary(row["customer_id"], row["full_name"], row["email"], row["phone"],
                   row["card_number"], date.fromisoformat(row["date_of_birth"]), row["marker"])
            for row in csv.DictReader(handle)
        ]  # fmt: skip
