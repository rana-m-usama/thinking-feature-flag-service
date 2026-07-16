# Local commands that mirror CI EXACTLY.
#
# The first CI run failed on `ruff format --check` because I had been running only
# `ruff check` locally. Two different checks, one of them never run until CI — which is
# how "works on my machine" gets built. `make lint` here runs precisely what
# .github/workflows/deploy.yml runs, so a green local lint means a green CI lint.

.PHONY: help dev lint fmt test up down migrate

help:
	@echo "  make up       Start Postgres + Redis (ports 5433/6380 to dodge native installs)"
	@echo "  make down     Stop them"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make dev      Run the API with reload"
	@echo "  make lint     EXACTLY what CI runs — check + format --check"
	@echo "  make fmt      Apply formatting"
	@echo "  make test     Run the suite (needs `make up` first)"

up:
	docker compose -f docker-compose.dev.yml up -d
	@echo "postgres :5433  redis :6380"

down:
	docker compose -f docker-compose.dev.yml down

migrate:
	.venv/bin/alembic upgrade head

dev:
	.venv/bin/uvicorn app.main:app --reload --port 8080

# Mirrors the Lint step in .github/workflows/deploy.yml. Keep these in lockstep.
lint:
	.venv/bin/ruff check app/ tests/
	.venv/bin/ruff format --check app/ tests/

fmt:
	.venv/bin/ruff format app/ tests/
	.venv/bin/ruff check --fix app/ tests/

test:
	.venv/bin/python -m pytest -q
