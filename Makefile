.PHONY: setup lint format typecheck test check up down demo benchmark

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

check: lint typecheck test

up:
	docker compose --profile core --profile sandbox up -d

down:
	docker compose --profile core --profile sandbox down

demo:
	@echo "Demo arrives with SandboxBank (B-02 to B-04)."

benchmark:
	@echo "Benchmark harness arrives with B-15."
