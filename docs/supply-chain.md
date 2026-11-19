# Supply chain: images, SBOMs and signatures

How RAI's container images are pinned, inventoried, scanned and signed (B-40: NFR-SEC-3; threat model T-08, T-10, E-05). Python dependencies follow the same rules through `uv.lock` (decisions DEP-COOLDOWN, DEP-MIN-AGE).

## The images

| Image | Dockerfile | What it is |
| --- | --- | --- |
| `rai-api` | `deploy/rai/Dockerfile` | The API (B-36) |
| `rai-caddy` | `deploy/caddy/Dockerfile` | Caddy with the web UI built in (B-36, B-37) |
| `rai-chaos-agent` | `chaos_agent/Dockerfile` | The chaos agent and its watchdog (B-23, B-25) |
| `rai-sandboxbank` | `sandboxbank/Dockerfile` | SandboxBank's payments API and ETL job (B-03) |

All four build from the repository root, whose `.dockerignore` lets in only the files they copy.

## What is pinned

- **Base images by digest.** Every `FROM` is `name:tag@sha256:…`, and so is every image in `compose.yaml` (PostgreSQL, Toxiproxy, the socket proxy). The tag is for people and Dependabot; the digest decides what is pulled, so a moved tag changes nothing (T-10).
- **Every Python package by hash.** Each Python image installs an export of `uv.lock` with `pip install --require-hashes --only-binary=:all:`, then its own package with `--no-deps --no-build-isolation`. The build backend (hatchling and its dependencies) comes from `deploy/build-requirements.txt`, also hash-pinned, so no image build resolves anything by itself.
- **The UI's packages from `package-lock.json`**, with `npm ci --ignore-scripts`.
- **Every GitHub Action by commit SHA**, in every workflow.
- **Nothing younger than 14 days**: uv's `exclude-newer`, `npm install --before`, and a 14-day Dependabot cooldown for every ecosystem, Docker included.

`tests/unit/test_images.py` and `tests/unit/test_tooling.py` fail if any of these slips.

## What CI does

The workflow **Images** (`.github/workflows/images.yml`):

| When | What |
| --- | --- |
| Every pull request | Builds the four images; records an SPDX SBOM for each (Syft, kept as a workflow artifact); fails on any **critical** vulnerability that has a fix (Grype). Read-only token, nothing pushed. |
| Every push to `main` | Builds and pushes to `ghcr.io/resilinet-ai/<image>`, tagged `sha-<commit>` and `main`, with SLSA provenance from BuildKit; records the SBOM of the pushed digest and scans it again; **only if the scan passes**, signs the digest with cosign (keyless: a short-lived Sigstore certificate for the workflow's GitHub identity, logged in Rekor) and attaches the SBOM as a signed attestation. |

Only the `main` job can write packages or request an OIDC token; pull requests cannot push or sign.

## Verifying an image

Use the digest, never a tag. The signer is the `images.yml` workflow on `main` of this repository:

```bash
IMAGE=ghcr.io/resilinet-ai/rai-api@sha256:<digest>
ID='https://github.com/resilinet-ai/resilinet-ai/.github/workflows/images.yml@refs/heads/main'
ISSUER=https://token.actions.githubusercontent.com

cosign verify "$IMAGE" --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER"
cosign verify-attestation "$IMAGE" --type spdxjson \
    --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" \
  | jq -r '.payload | @base64d | fromjson | .predicate.packages[].name' | sort -u
```

A signature from any other workflow, branch or repository fails the identity check.

## Updating a pin

Dependabot opens the pull requests: `docker` bumps the `FROM` lines, `docker-compose` the service images, `uv` and `npm` the packages, each after the 14-day cooldown. By hand:

```bash
docker buildx imagetools inspect python:3.12-slim --format '{{json .Manifest.Digest}}'
uv export --package rai-sandboxbank --no-dev --no-emit-workspace --format requirements.txt -o sandboxbank/requirements.txt
```

Each `requirements.txt` names its own export command in its header, and `deploy/build-requirements.txt` names its compile command.

## Not covered yet

- **Images in `compose.yaml` are built locally**, not pulled from GHCR, so `docker compose` does not verify signatures. Deploying the signed images, with verification before start, comes with B-18.
- **Third-party images** (PostgreSQL, Toxiproxy, the socket proxy, the Caddy and Node bases) are pinned by digest but not signature-checked: not all of them publish signatures.
- **Critical vulnerabilities without a fix** do not fail the build: there is nothing to upgrade to. The SBOM records the affected package, so a later scan finds it once a fix exists.
- **The build backend stays in the Python images** (it is in each SBOM); multi-stage builds would drop it.
