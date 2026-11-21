.PHONY: setup lint format typecheck test test-db test-db-stop check up down demo benchmark chaos-e2e tls-e2e journeys reproduce

setup:
	uv sync --all-packages
	uv run pre-commit install

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest --cov --cov-report=term-missing

# Throwaway PostgreSQL for integration tests; then:
# SANDBOXBANK_TEST_DSN=postgresql://ledger:sbx-test@127.0.0.1:55433/ledger_test make test
test-db:
	docker run -d --rm --name sbx-test-pg -e POSTGRES_USER=ledger -e POSTGRES_PASSWORD=sbx-test 		-e POSTGRES_DB=ledger_test -p 127.0.0.1:55433:5432 postgres:16

test-db-stop:
	docker stop sbx-test-pg

check: lint typecheck test

up:
	docker compose --profile core --profile sandbox up -d

down:
	docker compose --profile core --profile sandbox down

demo:
	uv run python scripts/demo_slice.py

benchmark:
	uv run rai-bench dry-run

# FR-07.1: each fault type on SandboxBank, through requests signed with a throwaway worker
# key in a fresh temporary folder (B-24); the agent trusts only that key. Then the
# kill-switch drill (B-25): the watchdog after the agent is killed, and kill --all.
chaos-e2e:
	key="$$(mktemp -d)/worker.pem"; export RAI_CHAOS_E2E_KEY="$$key" COMPOSE_PROJECT_NAME=rai-chaos-e2e; \
	RAI_AGENT_WORKER_KEYS="$$(uv run rai admin chaos-keygen --out "$$key")" ETL_INTERVAL_SECONDS=2 RAI_WATCHDOG_GRACE_SECONDS=3 docker compose --profile sandbox --profile chaos up -d --build --wait \
	&& uv run python scripts/chaos_e2e.py; status=$$?; docker compose --profile sandbox --profile chaos down -v; exit $$status

# NFR-SEC-2: the core profile behind Caddy, with throwaway credentials in a separate compose
# project (B-36): migrate, set the API role's password, start rai-api and Caddy, then check
# HTTPS, the headers, no plain HTTP and no direct port to rai-api.
tls-e2e:
	secret() { uv run python -c 'import secrets; print(secrets.token_urlsafe(36))'; }; \
	export COMPOSE_PROJECT_NAME=rai-tls-e2e RAI_DB_PASSWORD="$$(secret)" RAI_API_DB_PASSWORD="$$(secret)" RAI_JWT_SECRET="$$(secret)"; \
	export RAI_DATABASE_URL="postgresql://rai:$$RAI_DB_PASSWORD@127.0.0.1:55431/rai"; \
	export LEDGER_DB_PASSWORD=unused ANALYTICS_DB_PASSWORD=unused; \
	status=0; docker compose --profile core up -d --wait rai-db \
	&& uv run rai db upgrade && uv run rai db set-api-password \
	&& docker compose --profile core up -d --build --wait \
	&& uv run python scripts/tls_e2e.py || status=$$?; \
	[ $$status -eq 0 ] || docker compose --profile core logs --tail 40 rai-api caddy; \
	docker compose --profile core down -v; exit $$status

# B-46 (NFR-REP-2): journeys J1-J5 end to end, as the nightly job runs them. rai-db,
# SandboxBank and the chaos agent in Docker with throwaway credentials; the tests start
# the API and a worker themselves (tests/e2e/stack.py).
JOURNEY_SERVICES = rai-db ledger-db payments-api analytics-db etl-job toxiproxy socket-proxy rai-chaos-agent rai-chaos-watchdog
journeys:
	secret() { uv run python -c 'import secrets; print(secrets.token_urlsafe(36))'; }; \
	key="$$(mktemp -d)/worker.pem"; \
	export COMPOSE_PROJECT_NAME=rai-journeys RAI_E2E_WORKER_KEY="$$key" RAI_AGENT_ENVIRONMENT=SANDBOX-01 ETL_INTERVAL_SECONDS=2; \
	export RAI_DB_PASSWORD="$$(secret)" LEDGER_DB_PASSWORD="$$(secret)" ANALYTICS_DB_PASSWORD="$$(secret)" RAI_API_DB_PASSWORD=unused RAI_JWT_SECRET=unused; \
	export RAI_AGENT_WORKER_KEYS="$$(uv run rai admin chaos-keygen --out "$$key")"; \
	export RAI_E2E_DATABASE_URL="postgresql://rai:$$RAI_DB_PASSWORD@127.0.0.1:55431/rai"; \
	status=0; docker compose --profile core --profile sandbox --profile chaos up -d --build --wait $(JOURNEY_SERVICES) \
	&& uv run pytest tests/e2e -v || status=$$?; \
	docker compose --profile core --profile sandbox --profile chaos down -v; exit $$status

# B-41 (NFR-REP-2): reproduce a release on this machine, on a clean checkout of its tag:
# the locked install, the demo and the journeys, with a report of the machine, each step and
# the containers' peak memory (scripts/reproduce.py, docs/releasing.md).
reproduce:
	uv run --no-project python scripts/reproduce.py
