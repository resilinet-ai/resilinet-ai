# Hand over and verify evidence

**Goal:** give someone outside the team (an auditor, a partner, a reviewer) a self-contained record of what a run observed, which they can check offline without a RAI account (FR-13.1, FR-13.2).

**Who:** any member of the tenant exports. The verifier needs only RAI's command-line tool, or the published format, and your public key.

## Once: give the API a signing key (operator)

Without a key, bundles are exported unsigned and verifiers must accept that explicitly.

```bash
uv run rai admin evidence-keygen --out /run/secrets/rai_evidence_signing_key
# stdout: the public key; publish it where verifiers can trust it
```

Set `RAI_EVIDENCE_SIGNING_KEY=file:/run/secrets/rai_evidence_signing_key` for the API and restart it. Publish the public key somewhere the verifier already trusts, such as your release notes or a configuration repository, **not** only on the server whose bundles they will check.

## Export (any member)

In the UI: Runs, open a finished run, **Download evidence**. Or:

```bash
curl -s -OJ "$API/v1/assurance/runs/<run id>/evidence" -H "$AUTH"
# rai-evidence-<run id>.zip
```

The bundle holds the run with everything it was pinned to, every step and observation, lineage with data digests, recovery measurements, the approvals it ran under and its audit entries, plus a SHA-256 manifest and, with a key, a signature. It holds no data, only digests, and no secrets. Two exports of the same run are byte-identical.

## Check it holds no personal data (FR-05.1)

Before a bundle leaves the team, sweep it for the canary values of your sandbox (SandboxBank's `needles.csv`). Exit 0 means none was found; every hit is printed masked:

```bash
uv run rai canary scan --key data/canaries/needles.csv rai-evidence-<run id>.zip
# CLEAN   no canary value: ... lines and values in 1 source(s) searched ...
```

## Verify (the receiver)

```bash
uv run rai verify rai-evidence-<run id>.zip --key <your published public key>
# OK      manifest: every member matches (sha256 ...)
# OK      signature: signed by trusted key ...
# INFO    anchor: audit entry seq ..., record_hash ...; compare with the live chain
# VERIFIED rai-evidence-<run id>.zip
```

| Exit code | Meaning |
| --- | --- |
| `0` | Verified: every member matches the manifest, and the signature is by a key you passed |
| `1` | A member changed, was added or is missing; or the signature is missing, invalid, or by a key you did not pass |
| `2` | The bundle or a key could not be read |

An unsigned bundle fails (`1`) unless the verifier passes `--allow-unsigned`; the manifest is still checked.

## What it proves, and what it doesn't

- **Proves:** the bundle has not changed since export, and (signed) that your key signed it.
- **Does not prove** that the recorded facts were true. Evidence is tamper-evident, not tamper-proof (NFR-AUD-2).
- **The anchor** names the run's last audit entry. Someone with access to the live system can compare it with the audit chain (`rai audit verify`) to show the history was not rewritten after export.
- **Pin the key.** A valid signature by an unknown key fails on purpose: anyone who edits a bundle can re-sign it with their own key.

More: [evidence](../api.md#evidence), [audit chain](../audit-chain.md).
