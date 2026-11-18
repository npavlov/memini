SHELL := /bin/bash
.DEFAULT_GOAL := help

# `uv run` syncs the environment from uv.lock before each command.
UV_RUN := uv run

.PHONY: help sync lint format typecheck test check db db-down

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

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

test: ## Run tests (pytest)
	$(UV_RUN) pytest

check: lint typecheck test ## Everything a pull request must pass

db: ## Start the development database (Postgres with pgvector) and wait until healthy
	docker compose up -d --wait db

db-down: ## Stop the development database; the data volume stays
	docker compose down
