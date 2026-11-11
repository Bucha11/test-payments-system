.PHONY: up down logs test test-unit lint typecheck demo migrate

# Tests run on the host against Postgres/Redis published by compose.
TEST_ENV = DATABASE_URL=postgresql+asyncpg://cascade:cascade@localhost:5432/cascade \
           REDIS_URL=redis://localhost:6379/0

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
