"""Image hardening (B-40: NFR-SEC-3; threat model T-08, T-10).

These read the Dockerfiles, compose.yaml and the workflows as configuration. The CI
workflow "Images" builds every image, records its SBOM and scans it on each pull request,
and signs it on main.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DIGEST = r"@sha256:[0-9a-f]{64}"
DOCKERFILES = sorted(
    p.relative_to(ROOT).as_posix()
    for p in ROOT.glob("**/Dockerfile")
    if "node_modules" not in p.parts and ".venv" not in p.parts
)
PYTHON_IMAGES = ["chaos_agent/Dockerfile", "deploy/rai/Dockerfile", "sandboxbank/Dockerfile"]
COMPOSE: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
WORKFLOWS = ROOT / ".github" / "workflows"
IMAGES: dict[str, Any] = yaml.safe_load((WORKFLOWS / "images.yml").read_text(encoding="utf-8"))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_four_images_are_known() -> None:
    assert DOCKERFILES == [
        "chaos_agent/Dockerfile",
        "deploy/caddy/Dockerfile",
        "deploy/rai/Dockerfile",
        "sandboxbank/Dockerfile",
    ]


@pytest.mark.parametrize("dockerfile", DOCKERFILES)
def test_every_base_image_is_pinned_by_digest(dockerfile: str) -> None:
    # T-10: a tag can be moved to different content; a digest cannot. The tag stays for
    # people and Dependabot, the digest decides what is pulled.
    froms = re.findall(r"^FROM\s+(\S+)", read(dockerfile), flags=re.MULTILINE)
    assert froms
    for image in froms:
        assert re.fullmatch(r"[a-z0-9./-]+:[A-Za-z0-9._-]+" + DIGEST, image), image
    assert "TODO" not in read(dockerfile)


def test_every_compose_image_is_pinned_by_digest() -> None:
    for name, service in COMPOSE["services"].items():
        if "image" in service:
            assert re.search(DIGEST + "$", service["image"]), name
        else:  # built here, from the repository root, by a Dockerfile the workflow covers
            assert service["build"]["context"] == ".", name
            assert service["build"]["dockerfile"] in DOCKERFILES, name


def test_every_built_service_is_locked_down() -> None:
    # E-05: our own containers run read-only, with no capabilities beyond what they need and
    # no way to gain privileges (the images also run as a non-root user).
    built = {n: s for n, s in COMPOSE["services"].items() if "build" in s}
    assert set(built) == {
        "rai-api",
        "caddy",
        "payments-api",
        "etl-job",
        "rai-chaos-agent",
        "rai-chaos-watchdog",
        "rai-observer",
        "model-api",
    }
    for name, service in built.items():
        assert service["read_only"] is True, name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name


def test_the_recovery_observer_stands_outside_every_arm_and_every_fault() -> None:
    # B-44 (Protocol H3): no fault can target it (no rai.sandbox label), it cannot reach the
    # chaos agent or RAI, and it reads ledger-db directly rather than through Toxiproxy.
    observer = COMPOSE["services"]["rai-observer"]
    assert "rai.sandbox" not in observer.get("labels", {})
    assert observer["networks"] == ["sandbox_net"]
    assert "@ledger-db:5432/" in observer["environment"]["SANDBOXBANK_LEDGER_DSN"]
    assert observer["profiles"] == ["observer"]


@pytest.mark.parametrize("dockerfile", PYTHON_IMAGES)
def test_python_images_install_only_hash_pinned_packages(dockerfile: str) -> None:
    # Every package comes from a hash-pinned file, the build backend included: no pip
    # install may resolve anything by itself.
    text = read(dockerfile)
    installs = re.findall(r"pip install ([^\n&]+)", text)
    assert len(installs) == 2, installs
    locked, own = installs
    assert "--require-hashes" in locked
    assert "--only-binary=:all:" in locked
    assert "-r build-requirements.txt" in locked
    assert own.split() == ["--no-deps", "--no-build-isolation", "."]
    assert "COPY deploy/build-requirements.txt" in text
    assert "USER 10001" in text


def test_the_build_backend_is_pinned_with_hashes() -> None:
    text = read("deploy/build-requirements.txt")
    hashed = r"^([a-z0-9_.-]+)==\S+ \\\n((?:\s+--hash=sha256:[0-9a-f]{64}.*\n)+)"
    pins = re.findall(hashed, text, re.M)
    assert "hatchling" in {name for name, _ in pins}
    assert len(pins) == len(re.findall(r"^\S+==", text, re.M))  # every pin has its hashes
    assert ":/" not in text  # no local paths from the compile
    assert "\\Users" not in text


def test_every_action_in_every_workflow_is_pinned_to_a_commit() -> None:
    # T-08: a tag of a third-party action can be moved; a commit SHA cannot.
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for ref in re.findall(r"uses:\s*(\S+)", workflow.read_text(encoding="utf-8")):
            if ref.startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), (workflow.name, ref)


def test_the_images_workflow_covers_every_dockerfile() -> None:
    jobs = IMAGES["jobs"]
    for job in ("check", "publish"):
        built = sorted(i["dockerfile"] for i in jobs[job]["strategy"]["matrix"]["include"])
        assert built == DOCKERFILES, job
    assert jobs["check"]["strategy"]["matrix"] == jobs["publish"]["strategy"]["matrix"]


def test_every_image_gets_an_sbom_and_a_blocking_scan() -> None:
    for job in IMAGES["jobs"].values():
        steps = {s.get("uses", "").split("@")[0]: s for s in job["steps"]}
        assert steps["anchore/sbom-action"]["with"]["format"] == "spdx-json"
        scan = steps["anchore/scan-action"]["with"]
        assert scan["fail-build"] is True
        assert scan["severity-cutoff"] == "critical"


def test_only_main_can_push_and_sign_and_only_after_the_scan() -> None:
    assert IMAGES["permissions"] == {"contents": "read"}
    check, publish = IMAGES["jobs"]["check"], IMAGES["jobs"]["publish"]
    assert check["if"] == "github.event_name == 'pull_request'"
    assert "permissions" not in check  # read-only: pull requests never push or sign
    assert publish["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert publish["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    names = [s.get("name") or s["uses"].split("@")[0] for s in publish["steps"]]
    scan = names.index("No critical vulnerability with a fix (before signing)")
    sign = names.index("Sign the digest and attest the SBOM (keyless)")
    assert scan < sign
    script = publish["steps"][sign]["run"]
    assert 'cosign sign --yes "$IMAGE@$DIGEST"' in script  # the digest, never a tag
    assert "cosign attest --yes --type spdxjson" in script
    for step in (*check["steps"], *publish["steps"]):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False


def test_dependabot_keeps_the_digests_current() -> None:
    config = yaml.safe_load(read(".github/dependabot.yml"))
    by_ecosystem = {u["package-ecosystem"]: u for u in config["updates"]}
    folders = {"/" + Path(d).parent.as_posix() for d in DOCKERFILES}
    assert set(by_ecosystem["docker"]["directories"]) == folders
    assert by_ecosystem["docker-compose"]["directory"] == "/"


def test_dependabot_moves_the_vite_toolchain_together_and_keeps_pandas_below_3() -> None:
    config = yaml.safe_load(read(".github/dependabot.yml"))
    by_ecosystem = {u["package-ecosystem"]: u for u in config["updates"]}
    patterns = by_ecosystem["npm"]["groups"]["vite-toolchain"]["patterns"]
    assert {"vite", "vitest", "@vitejs/*"} <= set(patterns)
    ignored = {i["dependency-name"] for i in by_ecosystem["uv"]["ignore"]}
    assert "pandas" in ignored


def test_dependabot_never_edits_the_generated_requirements() -> None:
    # The image requirements are exports of uv.lock; an edit to one alone splits them.
    config = yaml.safe_load(read(".github/dependabot.yml"))
    uv = next(u for u in config["updates"] if u["package-ecosystem"] == "uv")
    generated = {
        p.relative_to(ROOT).as_posix()
        for p in ROOT.glob("**/*requirements.txt")
        if ".venv" not in p.parts and "node_modules" not in p.parts
    }
    assert generated
    assert generated <= set(uv["exclude-paths"])
