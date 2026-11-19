# Quick start

Two parts: validate a batch with one command and no server, then run the whole platform on your machine with its dashboard. Everything uses synthetic SandboxBank data.

**You need:** Python 3.12, [uv](https://docs.astral.sh/uv/), Docker with Compose, Git and, for part 2's dashboard, Node.js 24. The commands are for a POSIX shell (on Windows, Git Bash).

```bash
git clone https://github.com/resilinet-ai/resilinet-ai.git
cd resilinet-ai
make setup          # uv sync --all-packages, and the pre-commit hooks
```

## 1. Validate a batch (5 minutes, no server)

```bash
make demo
```

In about 15 seconds:

1. SandboxBank generates two batches of 100,000 synthetic card transactions.
2. The defect injector re-sends 100 rows of the first batch inside the second.
3. `rai validate` passes batch 1 (exit code `0`) and fails batch 2, naming `unique_across_batches:txn_id` and exactly those 100 rows (exit code `1`).
4. It runs again and checks that the JSON report is byte-identical.

The same by hand, on the batches the demo wrote to `data/slice/`. The key history remembers every batch it has seen, which is how a row re-sent in a *later* batch is caught, so start it empty and validate the batches in order:

```bash
rm -f .rai/history.sqlite
for batch in B000000 B000001; do
  uv run rai validate --contract contracts/sandboxbank/transactions_raw.yaml \
      --data data/slice/feed/transactions_raw/$batch.csv \
      --ref accounts=data/slice/feed/accounts.csv --ref merchants=data/slice/feed/merchants.csv \
      --history .rai/history.sqlite
  echo "$batch: exit code $?"
done
# ... B000000: exit code 0
# ... B000001: exit code 1
```

Point `--data` and the `--ref` files at your own CSV or Parquet files to validate them.

| Exit code | Meaning |
| --- | --- |
| `0` | The batch passes |
| `1` | A blocking rule failed or could not run |
| `2` | The command could not run, for example a bad contract or a missing file |

`--format json` (or `--report FILE`) gives the deterministic JSON report. A contract is a YAML file of rules with severities and thresholds: see [contracts/README.md](../contracts/README.md).

## 2. Run the platform (15 minutes)

You will run the RAI database in Docker, and the API and the worker on your machine; seed a project with two runs; and open the dashboard.

**Configure.** Copy the example settings and choose your own values:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste as RAI_JWT_SECRET in .env
```

Change every `change-me-locally` in `.env`. Each password appears twice (as `..._PASSWORD` and inside the matching `..._URL`); keep the pairs equal. `.env` is git-ignored. The `rai` commands below read it with `uv run --env-file .env`.

**Start the database and create a tenant:**

```bash
docker compose --profile core up -d --wait rai-db       # PostgreSQL on 127.0.0.1:55431
uv run --env-file .env rai db upgrade                   # the schema, as the owner role
uv run --env-file .env rai db set-api-password          # the API's login role (rai_api)
uv run --env-file .env rai db set-worker-password       # the worker's login role (rai_worker)

export RAI_NEW_PASSWORD='choose a long local password'  # 12 characters or more
uv run --env-file .env rai admin create-tenant --slug demo-cu --name "Demo CU" \
    --admin-email admin@example.test --admin-name "Demo Admin"
```

**Start the API and the worker**, each in its own terminal, in the repository folder:

```bash
uv run --env-file .env rai serve                        # terminal 1: the API on 127.0.0.1:8000

uv run python -m scripts.quickstart data .rai/quickstart                       # terminal 2: demo batches
RAI_WORKER_CONNECTORS=.rai/quickstart/connectors uv run --env-file .env rai worker
```

Both log JSON lines; add `RAI_LOG_FORMAT=text` in front of a command for readable lines ([observability](observability.md)).

**Seed a project.** In a third terminal, with `RAI_NEW_PASSWORD` set as before:

```bash
uv run --env-file .env python -m scripts.quickstart seed
# Project 'Quick start' created; runs queued:
#   clean   01a1f3bf-...
#   resent  01a1f3bf-...
```

Through the API, this registers the environment `SANDBOX-01`, invites `quickstart-engineer@example.test` and `quickstart-reviewer@example.test` (with your password), publishes the SandboxBank contract on two batches, and starts two runs. The worker executes them in a few seconds: `clean` **passes**; `resent`, whose second batch re-sends two rows of the first, **fails** on `unique_across_batches:txn_id`.

**Open the dashboard:**

```bash
cd web
npm ci                 # exact versions from package-lock.json
npm run dev            # http://127.0.0.1:5173, forwarding /v1 to the API
```

Sign in as `admin@example.test` with your password. The Dashboard shows the validation pass rate with its denominator, the failing rule, and both runs; Runs opens each run, with its failing rows and an evidence download.

## Next

- Gate a pull request on these runs, open a finding on the failure and waive it: [playbooks](playbooks/index.md).
- Run RAI as it is deployed, behind TLS through Caddy: [deployment](deployment.md).
- Run a chaos scenario against SandboxBank: [run a chaos scenario](playbooks/chaos-scenario.md).

## Clean up

```bash
docker compose --profile core down -v      # stops the database and deletes its data
rm -rf .rai/quickstart
```
