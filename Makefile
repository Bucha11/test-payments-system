SHELL := /bin/bash

.PHONY: up down logs test test-unit lint typecheck demo demo-save

# Tests run on the host against Postgres/Redis published by compose, in their own
# database / Redis db, so a running worker never touches test data.
TEST_ENV = DATABASE_URL=postgresql+asyncpg://cascade:cascade@localhost:5432/cascade_test \
           TEST_REDIS_URL=redis://localhost:6379/15

.env:
	cp .env.example .env

up: .env
	docker compose up -d --build --wait

down:
	docker compose down -v

logs:
	docker compose logs -f api worker

test: .env
	docker compose up -d --wait postgres redis
	docker compose exec -T postgres psql -U cascade -tAc \
	  "SELECT 1 FROM pg_database WHERE datname='cascade_test'" | grep -q 1 \
	  || docker compose exec -T postgres createdb -U cascade cascade_test
	$(TEST_ENV) uv run alembic upgrade head
	$(TEST_ENV) uv run pytest -q

test-unit:
	uv run pytest -q tests/unit

lint:
	uv run ruff check src tests migrations scripts
	uv run ruff format --check src tests migrations scripts

typecheck:
	uv run mypy

demo:
	uv run python scripts/demo.py

# Records a real run into docs/demo-output.txt (embedded in the README).
demo-save:
	set -o pipefail; uv run python scripts/demo.py 2>&1 | tee docs/demo-output.txt
