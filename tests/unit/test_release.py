"""Releases (B-41: NFR-REP-2, NFR-SEC-3): one version, a guarded tag, signed artefacts.

``scripts/release.py`` keeps every version in step and decides whether a tag may be
released; the workflow "Release" runs it first. ``scripts/reproduce.py`` is the one
command that reproduces a release and reports on the machine it ran on.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import release, reproduce

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE: dict[str, Any] = yaml.safe_load((WORKFLOWS / "release.yml").read_text("utf-8"))
IMAGES: dict[str, Any] = yaml.safe_load((WORKFLOWS / "images.yml").read_text("utf-8"))
VERSIONED = [
    *release.PYPROJECTS.values(),
    "uv.lock",
    release.PACKAGE_JSON,
    release.PACKAGE_LOCK,
]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel in VERSIONED:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, tmp_path / rel)
    return tmp_path


def notes(root: Path, tag: str, text: str) -> None:
    path = root / release.notes_path(tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_repository_states_one_version() -> None:
    found = release.versions()
    assert len(found) == 11  # four pyprojects, their four lock entries, the UI's three
    assert len(set(found.values())) == 1, found


def test_bump_changes_exactly_the_version_lines(repo: Path) -> None:
    before = {rel: (repo / rel).read_text(encoding="utf-8").splitlines() for rel in VERSIONED}
    old = release.versions(repo)["pyproject.toml"]
    release.bump("1.0.0", repo)
    assert set(release.versions(repo).values()) == {"1.0.0"}
    for rel, lines in before.items():
        after = (repo / rel).read_text(encoding="utf-8").splitlines()
        assert len(after) == len(lines), rel
        changed = [(a, b) for a, b in zip(lines, after, strict=True) if a != b]
        expected = 4 if rel == "uv.lock" else 2 if rel == release.PACKAGE_LOCK else 1
        assert len(changed) == expected, rel
        for a, b in changed:
            assert "version" in a
            assert a.replace(old, "1.0.0") == b


def test_bump_refuses_a_version_that_is_not_semver(repo: Path) -> None:
    with pytest.raises(ValueError, match=r"MAJOR\.MINOR\.PATCH"):
        release.bump("1.0", repo)


def test_a_tag_needs_matching_versions_and_final_notes(repo: Path) -> None:
    assert release.check("1.0.0", repo) == [
        "1.0.0: a release tag is vMAJOR.MINOR.PATCH, for example v1.0.0"
    ]
    problems = release.check("v1.0.0", repo)
    assert len(problems) == 12  # eleven stale versions and no notes
    assert problems[-1].startswith("docs/releases/v1.0.0.md is missing")

    release.bump("1.0.0", repo)
    notes(repo, "v1.0.0", "# RAI v1.0.0\n\n> Status: draft. Not yet.\n")
    assert release.check("v1.0.0", repo) == ['docs/releases/v1.0.0.md still says "Status: draft"']

    notes(repo, "v1.0.0", "# RAI v1.0.0\n\n## Limitations\n")
    assert release.check("v1.0.0", repo) == []
    assert release.check("v1.0.1", repo)  # a different tag still does not match


def test_the_v1_notes_are_a_draft_until_the_day_90_conditions_hold() -> None:
    # The tag cannot be released by accident: the notes stay marked as a draft until the
    # release pull request (docs/releasing.md, step 2), which also removes this test.
    text = (ROOT / release.notes_path("v1.0.0")).read_text(encoding="utf-8")
    assert release.DRAFT in text
    assert "## Limitations" in text
    assert release.check("v1.0.0") != []


def test_the_release_runs_only_on_a_version_tag_and_reads_by_default() -> None:
    assert RELEASE[True] == {"push": {"tags": ["v*.*.*"]}}  # YAML reads "on" as True
    assert RELEASE["permissions"] == {"contents": "read"}
    assert RELEASE["concurrency"]["cancel-in-progress"] is False


def test_the_tag_is_checked_before_anything_is_built_or_signed() -> None:
    steps = RELEASE["jobs"]["prepare"]["steps"]
    names = [s.get("name") or s["uses"].split("@")[0] for s in steps]
    guards = [
        names.index("The tag matches every version and has final release notes"),
        names.index("The tagged commit is on main"),
        names.index("The lock is current"),
    ]
    build = names.index("Build the Python packages")
    assert max(guards) < build
    assert 'scripts/release.py check "$TAG"' in steps[guards[0]]["run"]
    assert "git merge-base --is-ancestor" in steps[guards[1]]["run"]
    assert steps[0]["with"]["fetch-depth"] == 0  # origin/main is needed for the ancestor check
    sign = steps[names.index("Checksums, signed keylessly")]["run"]
    assert "cosign sign-blob --yes --bundle SHA256SUMS.sigstore.json SHA256SUMS" in sign
    draft = steps[names.index("Draft the release")]["run"]
    assert "--draft" in draft  # published by hand, after the clean-laptop reproduction
    assert '--notes-file "docs/releases/$TAG.md"' in draft


def test_release_images_are_built_scanned_and_signed_as_on_main() -> None:
    jobs = RELEASE["jobs"]
    images = jobs["images"]
    assert images["needs"] == "prepare"
    assert jobs["digests"]["needs"] == "images"
    assert images["strategy"]["matrix"] == IMAGES["jobs"]["publish"]["strategy"]["matrix"]
    names = [s.get("name") or s["uses"].split("@")[0] for s in images["steps"]]
    scan = names.index("No critical vulnerability with a fix (before signing)")
    sign = names.index("Sign the digest and attest the SBOM (keyless)")
    assert scan < sign
    assert images["steps"][scan]["with"]["fail-build"] is True
    script = images["steps"][sign]["run"]
    assert 'cosign sign --yes "$IMAGE@$DIGEST"' in script  # the digest, never a tag
    assert "cosign attest --yes --type spdxjson" in script
    listed = jobs["digests"]["steps"][-1]["run"]
    for entry in images["strategy"]["matrix"]["include"]:
        assert entry["name"] in listed  # images.txt names every release image


def test_only_the_jobs_that_need_it_can_write_or_sign() -> None:
    jobs = RELEASE["jobs"]
    assert jobs["prepare"]["permissions"] == {"contents": "write", "id-token": "write"}
    assert jobs["images"]["permissions"] == {
        "contents": "write",
        "packages": "write",
        "id-token": "write",
    }
    assert jobs["digests"]["permissions"] == {"contents": "write", "packages": "read"}
    for job in jobs.values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize(
    ("text", "size"),
    [("0B", 0), ("512KiB", 512 * 1024), ("1.5GiB", 3 * 1024**3 // 2), ("20.3MB", 20_300_000)],
)
def test_docker_stats_sizes(text: str, size: int) -> None:
    assert reproduce.parse_size(text) == size


def test_a_size_without_a_known_unit_is_refused() -> None:
    with pytest.raises(ValueError, match="not a size"):
        reproduce.parse_size("12 parsecs")


def test_container_memory_is_the_sum_of_what_each_uses() -> None:
    lines = "100MiB / 7.6GiB\n1GiB / 7.6GiB\n\n"
    assert reproduce.containers_in_use(lines) == 100 * 1024**2 + 1024**3


@pytest.mark.parametrize(
    ("memory", "eight_gb"),
    [(8 * 1024**3, True), (7_900_000_000, True), (8 * 1024**3 + 1, False), (None, False)],
)
def test_an_8_gb_machine_has_at_most_8_gib(memory: int | None, eight_gb: bool) -> None:
    assert reproduce.Machine("Linux", 4, memory, None).eight_gb is eight_gb


def _report(exit_codes: list[int], memory: int) -> str:
    steps = [
        reproduce.Step(f"step {i}", ["make", f"s{i}"], seconds=12.4, exit_code=code)
        for i, code in enumerate(exit_codes)
    ]
    host = reproduce.Machine("Linux-6.8", 4, memory, 4 * 1024**3, {"uv": "uv 0.13.8"})
    when = datetime(2027, 2, 13, 9, 30, tzinfo=UTC)
    return reproduce.report(when, "abc123", "v1.0.0", True, host, steps, 3 * 1024**3)


def test_the_report_passes_only_when_every_step_passed() -> None:
    passed = _report([0, 0, 0], 8 * 1024**3)
    assert "- **Result:** PASS" in passed
    assert "(an 8 GB machine)" in passed
    assert "**Containers' peak memory during the journeys:** 3.0 GiB" in passed
    assert "| step 2 | `make s2` | pass | 12 s |" in passed
    assert "| uv | uv 0.13.8 |" in passed

    failed = _report([0, 2, 0], 16 * 1024**3)
    assert "- **Result:** FAIL" in failed
    assert "| step 1 | `make s1` | FAIL (exit code 2) | 12 s |" in failed
    assert "**not** an 8 GB machine" in failed
