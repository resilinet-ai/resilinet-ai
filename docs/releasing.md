# Releasing RAI

How a version of RAI is released, verified and reproduced (B-41: NFR-REP-2, NFR-SEC-3). v1.0 is due on day 90 (2027-02-13), and it comes **after** the protocol is registered and **before** any benchmark run (Build Plan, section 9).

## What a release is

- **One version for the repository:** the four Python packages, their entries in `uv.lock`, and the web UI's `package.json` and `package-lock.json`.
- **A signed tag `vMAJOR.MINOR.PATCH`** on a commit of `main`, pushed by the author.
- **Release notes** in `docs/releases/<tag>.md`.
- **A GitHub release** with:
  - the packages, their `SHA256SUMS` and its Sigstore bundle;
  - the four images by digest (`images.txt`), each signed, with its SPDX SBOM;
  - the reproduction report from a clean 8 GB laptop.

## The procedure

1. **The conditions hold.** For v1.0:
   - baselines B1 and B2 are frozen;
   - the protocol is registered;
   - the S01–S20 registry is sealed (B-28).
2. **Release pull request.** On a branch:
   - run `python scripts/release.py bump 1.0.0`;
   - finish `docs/releases/v1.0.0.md` and remove its "Status: draft" line;
   - run `python scripts/release.py check v1.0.0`; it must say the tag may be released.

   Then merge the pull request.
3. **Tag `main`, signed, and push the tag:**

   ```bash
   git switch main && git pull --ff-only
   git tag -s v1.0.0 -m "RAI v1.0.0"
   git push origin v1.0.0
   ```

4. **The workflow "Release"** (`.github/workflows/release.yml`) runs on the tag.
   - It refuses the tag unless:
     - `scripts/release.py check` passes;
     - the commit is on `main`;
     - `uv.lock` is current.
   - It builds the packages, and signs their checksums keylessly.
   - It builds and pushes the four images, tagged with the version. Each one gets an SBOM and the blocking scan, and only then a signature, as on `main`.
   - It collects everything in a **draft** release.
5. **Reproduce on a clean 8 GB laptop.** On a machine with at most 8 GiB of memory, and nothing of RAI on it yet, set up the [quick start](quickstart.md) prerequisites. Then run:

   ```bash
   git clone --branch v1.0.0 https://github.com/resilinet-ai/resilinet-ai.git
   cd resilinet-ai
   make reproduce
   ```

   `make reproduce` runs these steps, then writes `reproduction-v1.0.0.md`:
   - the locked install;
   - `make demo`;
   - `make journeys`, which runs the whole stack and journeys J1–J5, with a real chaos fault.

   The report records:
   - the machine, the tools, and the commit and tag;
   - each step's result and time;
   - the containers' peak memory.

   It must say **PASS** and **an 8 GB machine**.
6. **Publish.**
   - Attach the report to the draft release: `gh release upload v1.0.0 reproduction-v1.0.0.md`.
   - Check the attachments with the commands below.
   - Publish the release.

If anything fails after the tag is pushed, delete the draft release and the tag, fix the cause on `main`, and tag again. A published version is never re-tagged: the next one is a patch release.

## Verify a release

The signer of everything in a release is the workflow "Release", on that tag, of this repository:

```bash
TAG=v1.0.0
ID="https://github.com/resilinet-ai/resilinet-ai/.github/workflows/release.yml@refs/tags/$TAG"
ISSUER=https://token.actions.githubusercontent.com
gh release download "$TAG" --repo resilinet-ai/resilinet-ai --dir "rai-$TAG" && cd "rai-$TAG"

# The packages
cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \
    --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER"
sha256sum --check SHA256SUMS

# The images, by digest
while read -r image; do
  cosign verify "$image" --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER"
  cosign verify-attestation "$image" --type spdxjson \
      --certificate-identity "$ID" --certificate-oidc-issuer "$ISSUER" > /dev/null
done < images.txt

# The tag, signed by the author
git verify-tag "$TAG"
```

The images published from `main` between releases have a different signer, the workflow "Images" ([supply chain](supply-chain.md)).

## What "an 8 GB laptop" means

NFR-REP-2 asks for a one-command stack on an 8 GB laptop.

- **The command** is `make reproduce`.
- **The machine:** the report calls it "an 8 GB machine" when its physical memory is at most 8 GiB. Laptops sold as 8 GB report slightly less.
- **Docker's memory:** Docker Desktop gives its virtual machine only part of that, and the report records how much.
- **The peak memory figure** counts the containers. The API, the worker and the tests run on the host and are not in it.
