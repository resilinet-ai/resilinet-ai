"""Canary sweeps of files (FR-05.1, part 3): logs and evidence bundles.

A ``privacy_canary`` test searches a data asset during a run. A sweep searches what a
run leaves behind instead: RAI's own logs, the sandbox services' logs, and evidence
bundles. It uses the same answer key and the same search (every needle at once, as a
substring, ignoring ASCII case; overlapping matches reported once, as the longest),
and the same rule: a hit names where it was found, never the value in full.

What is searched, and how a hit's place is given:

* **a text file** (a log, JSON lines, anything else): each line, ``line 12``;
* **a ``.gz`` file**: the same, decompressed;
* **a ``.json`` file**: every key, string and number, by JSON path (``$.steps[0].error``),
  so a card number stored as a number still counts. A file that does not parse is
  read as lines;
* **a ``.zip`` file** (an evidence bundle): each member as above, the source given as
  ``bundle.zip!rai-evidence-<run>/steps.json``;
* **a directory**: every file in it, recursively, in name order;
* **``-``**: standard input, as lines (``docker compose logs | rai canary scan ...``).

Fail closed: a source that cannot be read, or a key that cannot work, makes the sweep
an error, never a pass. The key file itself is skipped if it is among the sources.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import polars as pl

from rai.privacy.canary import MAX_SAMPLE, Needle, find, mask, needles

CHUNK = 20_000  # pieces of text searched at once
STDIN = "-"


@dataclass(frozen=True)
class Finding:
    source: str  # a file, or ``bundle.zip!member``
    place: str  # ``line 12``, or a JSON path
    canary: str
    kind: str
    masked: str

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "place": self.place, "canary": self.canary,
                "kind": self.kind, "value": self.masked}  # fmt: skip


@dataclass
class Sweep:
    needles: int = 0
    canaries: int = 0
    sources: int = 0
    pieces: int = 0  # lines and JSON values searched
    hits: int = 0
    sample: list[Finding] = field(default_factory=list)
    by_kind: Counter[str] = field(default_factory=Counter)
    by_source: Counter[str] = field(default_factory=Counter)
    by_canary: Counter[str] = field(default_factory=Counter)
    unreadable: list[str] = field(default_factory=list)  # ``source: why``
    skipped: list[str] = field(default_factory=list)
    error: str | None = None  # the key cannot be used

    @property
    def outcome(self) -> str:
        if self.error or self.unreadable:
            return "error"
        return "fail" if self.hits else "pass"

    def summary(self) -> str:
        if self.error:
            return self.error
        searched = (f"{self.pieces:,} lines and values in {self.sources} source(s) searched "
                    f"for {self.needles} needles of {self.canaries} canaries")  # fmt: skip
        if self.unreadable:
            return f"{len(self.unreadable)} source(s) could not be read; {searched}"
        if not self.hits:
            return f"no canary value: {searched}"
        return (f"{self.hits} canary value(s) of {len(self.by_canary)} canary(ies) found in "
                f"{len(self.by_source)} source(s); {searched}")  # fmt: skip

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "summary": self.summary(), "needles": self.needles,
            "canaries": self.canaries, "sources": self.sources, "pieces": self.pieces,
            "hits": self.hits, "by_kind": dict(self.by_kind),
            "by_source": dict(self.by_source), "by_canary": dict(self.by_canary),
            "sample": [f.as_dict() for f in self.sample], "unreadable": self.unreadable,
            "skipped": self.skipped, "error": self.error,
        }  # fmt: skip


def load_key(path: Path) -> tuple[list[Needle], str | None]:
    """The needles of a key file: ``needles.csv`` (``canary, kind, value``) or
    SandboxBank's ``canaries.json``; or why it cannot be used."""
    try:
        if path.suffix.lower() == ".json":
            answer = json.loads(path.read_text(encoding="utf-8"))
            rows = [(c["customer_id"], kind, value) for c in answer["canaries"]
                    for kind, value in c["needles"].items()]  # fmt: skip
            frame = pl.DataFrame(rows, schema=["canary", "kind", "value"], orient="row")
        else:
            frame = pl.read_csv(path, infer_schema=False)
    except (OSError, ValueError, KeyError, TypeError, pl.exceptions.PolarsError) as error:
        return [], f"the canary key {path.name} cannot be read ({type(error).__name__})"
    return needles(frame)


def _lines(text: str) -> Iterator[tuple[str, str]]:
    for number, line in enumerate(text.splitlines(), start=1):
        if line:
            yield f"line {number}", line


def _json_values(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield f"{path}.{key}(key)", str(key)
            yield from _json_values(inner, f"{path}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _json_values(inner, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield path, str(value)


def _text(name: str, data: bytes) -> Iterator[tuple[str, str]]:
    if name.lower().endswith(".gz"):
        data = gzip.decompress(data)
        name = name[:-3]
    text = data.decode("utf-8", errors="replace")
    if name.lower().endswith(".json"):
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        else:
            yield from _json_values(parsed)
            return
    yield from _lines(text)


def _pieces(path: Path, label: str) -> Iterator[tuple[str, str, str]]:
    """(source, place, text) for every piece of text in one file."""
    data = path.read_bytes()
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in sorted(archive.namelist()):
                if not member.endswith("/"):
                    for place, text in _text(member, archive.read(member)):
                        yield f"{label}!{member}", place, text
        return
    for place, text in _text(path.name, data):
        yield label, place, text


def _files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file())
    return [path]


def sweep(
    sources: Iterable[str], found: list[Needle], *, case_sensitive: bool = False,
    key_path: Path | None = None, stdin: TextIO | None = None,
) -> Sweep:  # fmt: skip
    """Search every source for every needle; every hit masked."""
    result = Sweep(needles=len(found), canaries=len({n.canary for n in found}))
    fold = (lambda v: v) if case_sensitive else str.lower
    lookup = {fold(n.value): n for n in found}
    skip = key_path.resolve() if key_path else None
    batch: list[tuple[str, str, str]] = []

    def search() -> None:
        frame = pl.DataFrame({"text": [text for _, _, text in batch]}, schema={"text": pl.String})
        for row, values in find(frame, "text", found, case_sensitive=case_sensitive):
            source, place, _ = batch[row]
            for value in values:
                needle = lookup[fold(value)]
                result.hits += 1
                result.by_kind[needle.kind] += 1
                result.by_source[source] += 1
                result.by_canary[needle.canary] += 1
                if len(result.sample) < MAX_SAMPLE:
                    result.sample.append(
                        Finding(source, place, needle.canary, needle.kind, mask(needle.value))
                    )
        result.pieces += len(batch)
        batch.clear()

    def take(pieces: Iterator[tuple[str, str, str]]) -> None:
        for piece in pieces:
            batch.append(piece)
            if len(batch) >= CHUNK:
                search()

    for source in sources:
        if source == STDIN:
            result.sources += 1
            text = stdin.read() if stdin else ""
            take(("<stdin>", place, line) for place, line in _lines(text))
            continue
        root = Path(source)
        if not root.exists():
            result.unreadable.append(f"{source}: not found")
            continue
        for path in _files(root):
            label = path.as_posix()
            if skip is not None and path.resolve() == skip:
                result.skipped.append(f"{label}: the canary key itself")
                continue
            result.sources += 1
            try:
                take(_pieces(path, label))
            except (OSError, zipfile.BadZipFile, EOFError, gzip.BadGzipFile) as error:
                result.unreadable.append(f"{label}: {type(error).__name__}: {error}")
    if batch:
        search()
    return result
