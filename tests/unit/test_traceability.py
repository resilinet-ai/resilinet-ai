import pytest

from scripts.check_traceability import check_title, known_ids


@pytest.fixture(scope="module")
def ids() -> set[str]:
    return known_ids()


def test_csv_lists_every_mvp_requirement(ids: set[str]) -> None:
    for required in ("FR-03.3", "FR-07.2", "FR-08.1", "FR-13.1", "NFR-SAF-1"):
        assert required in ids


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat(FR-03.3): detect duplicate keys across batches", True),
        ("fix(NFR-SAF-1): refuse unknown environments", True),
        ("docs: add ADR-011", True),
        ("chore: bump ruff", True),
        ("feat: add something untraced", False),
        ("feat(FR-99.9): not in the spec", False),
    ],
)
def test_check_title(title: str, expected: bool, ids: set[str]) -> None:
    ok, _ = check_title(title, ids)
    assert ok is expected
