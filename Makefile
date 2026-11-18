SHELL := /bin/bash
.DEFAULT_GOAL := help

# `uv run` syncs the environment from uv.lock before each command.
UV_RUN := uv run

.PHONY: help sync lint format typecheck test test-integration check eval db db-down smoke

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-18s %s\n", $$1, $$2}'

sync: ## Install dependencies into .venv
	uv sync

lint: ## Check lint and formatting (ruff)
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

format: ## Format and apply safe lint fixes (ruff)
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

typecheck: ## Type check (pyright, strict)
	$(UV_RUN) pyright

test: ## Run unit tests (pytest); no database needed
	$(UV_RUN) pytest -m "not integration"

test-integration: ## Run integration tests against Postgres (make db, or MEMINI_TEST_DATABASE_URL)
	$(UV_RUN) pytest -m integration

check: lint typecheck test ## Everything a pull request must pass

eval: ## Retrieval eval, FakeEmbedder, against the committed baseline (MEMINI_DATABASE_URL, CREATEDB)
	$(UV_RUN) memini eval --fake-embeddings --baseline eval/code/baseline-fake.json

db: ## Start the development database (Postgres with pgvector) and wait until healthy
	docker compose up -d --wait db

db-down: ## Stop and remove the compose containers (database and service); the data volume stays
	docker compose down

smoke: ## M0 check: compose up (service and database), then MCP status from the host with a token
	scripts/compose-smoke.sh
