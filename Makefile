.PHONY: setup lint format typecheck test test-db test-db-stop check up down demo benchmark chaos-e2e

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

# FR-07.1: each fault type on SandboxBank (unsigned dev requests: local and CI only).
chaos-e2e:
	RAI_AGENT_UNSIGNED_DEV=1 ETL_INTERVAL_SECONDS=2 COMPOSE_PROJECT_NAME=rai-chaos-e2e docker compose --profile sandbox --profile chaos up -d --build --wait
	COMPOSE_PROJECT_NAME=rai-chaos-e2e uv run python scripts/chaos_e2e.py; status=$$?; COMPOSE_PROJECT_NAME=rai-chaos-e2e docker compose --profile sandbox --profile chaos down -v; exit $$status
