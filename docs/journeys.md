# User journeys, end to end

The five user journeys of the specification (section 13) are acceptance tests, in `tests/e2e/test_journeys.py` (B-46, NFR-REP-2). They run every night in the workflow "Journeys", and on any pull request that changes them or `compose.yaml`.

## What runs

- **In Docker:** rai-db, SandboxBank (ledger-db, payments-api, analytics-db, etl-job, Toxiproxy) and the chaos agent with its watchdog and socket proxy.
- **As processes:** the tests start `rai serve` and `rai worker`.
- **How the tests drive RAI:** only as its users do, through the HTTP API, the `rai` CLI (`rai run`, `rai verify`, `rai audit verify`) and the operator commands (`rai db upgrade`, `rai admin create-tenant`, `rai admin set-password`).

The journeys share one stack and run in order, because each builds on the one before. If an earlier journey did not get far enough, the later ones are skipped, not failed.

| Journey | What the test does | What it checks |
| --- | --- | --- |
| J1 Onboard a sandbox | <ul><li>The admin invites an engineer, two reviewers and a viewer.</li><li>The admin registers `SANDBOX-01` and its allow-list. The container is pinned to its running image, read with `docker inspect`.</li><li>The worker's connectors reach SandboxBank's databases through `env:` secret references.</li><li>The engineer creates a project classified `synthetic`.</li></ul> | <ul><li>Only an admin can register an environment.</li><li>A container target without an image is refused.</li><li>No credential is in a connector file.</li><li>Every step is in the audit chain, and `rai audit verify` passes.</li></ul> |
| J2 Validate in CI | <ul><li>The engineer publishes the `transactions_raw` contract on two batches.</li><li>`rai run` runs exactly as the GitHub Action calls it, with an API key and `GITHUB_OUTPUT`.</li></ul> | <ul><li>The clean plan exits 0.</li><li>The plan whose second batch re-sends rows exits 1, failing only `unique_across_batches:txn_id`.</li><li>The failing run has observations and lineage.</li></ul> |
| J3 Controlled chaos | <ul><li>The engineer defines `ledger-db-connection-loss`: a 5 s connection drop on `ledger-db-proxy`, RTO 60 s, RPO 0, and a consistency check of the warehouse against the ledger.</li><li>A reviewer approves it.</li><li>The run happens while SandboxBank's seeder posts writes.</li></ul> | <ul><li>The author cannot approve their own scenario.</li><li>The run passes both preflights, injects and pins the image.</li><li>The RTO, RPO and the four consistency findings all pass.</li><li>The agent holds no open fault afterwards.</li></ul> |
| J4 Decide a release | <ul><li>The engineer opens a critical finding on J2's failure.</li><li>The engineer asks for a 14-day waiver, and a reviewer approves it.</li><li>The second reviewer approves release `v1.0`.</li></ul> | <ul><li>The gate is blocked, both for the release and for `rai run`.</li><li>The approval is refused while the gate is blocked.</li><li>Once the waiver is in force, the gate passes.</li><li>The release decision is in the audit chain.</li></ul> |
| J5 Reproduce evidence | <ul><li>The reviewer exports J2's and J3's bundles.</li><li>An outside researcher works in their own tenant. They rebuild the plan from the bundle's pins, use the contract from this repository, regenerate the data from the same seeds, and run `rai run`.</li></ul> | <ul><li>The bundles are byte-identical on every export.</li><li>`rai verify --key` passes them offline and fails a tampered copy.</li><li>The contract hash matches the pin, and the data matches byte for byte.</li><li>The verdicts, the per-step results, the observations and the manifest's list of files all match.</li></ul> |

## Run them locally

```bash
make journeys
```

`make journeys` needs Docker and about 3 GB of free memory. It generates throwaway credentials and a worker key, starts the services, runs the tests and removes the stack again. To run the tests against a stack you started yourself, set:

- `RAI_E2E_DATABASE_URL`: rai-db as its owner;
- `RAI_E2E_WORKER_KEY`: the worker key the agent trusts;
- `LEDGER_DB_PASSWORD` and `ANALYTICS_DB_PASSWORD`.

Then run `uv run pytest tests/e2e`. Without `RAI_E2E_DATABASE_URL`, the journeys are skipped. The ordinary test job runs that way.

## What "matching manifest" means in J5

A re-run is a different run, with its own run ID, timestamps and audit entries. Its bundle therefore cannot be byte-identical to the original. J5 instead requires that:

- the files listed in the manifest are the same;
- every step ends in the same state with the same verdict;
- every observation has the same rule, outcome and observed value.

The data and the contract must also match their pins: the regenerated batches byte for byte, and the contract by its SHA-256.
