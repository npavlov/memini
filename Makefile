SHELL := /bin/bash
.DEFAULT_GOAL := help

# `uv run` syncs the environment from uv.lock before each command.
UV_RUN := uv run

# Extra arguments of `memini eval` for the eval targets, e.g. EVAL_ARGS="--json report.json".
EVAL_ARGS ?=
# The margin of eval-qwen3. Hybrid results move by up to about 0.03 from run to run until tied
# lexical results are ordered by something stable across runs (docs/requirements.md, section 10).
QWEN3_MARGIN ?= 0.05

.PHONY: help sync lint format typecheck test test-integration check eval eval-qwen3 db db-down smoke

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-18s %s\n", $$1, $$2}'

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

.PHONY: backup-test
backup-test: ## Dump a filled test database, restore it into an empty one and compare (Postgres as above)
	$(UV_RUN) pytest -m integration tests/test_backup_db.py

check: lint typecheck test ## Everything a pull request must pass

.PHONY: config-docs
config-docs: ## Regenerate docs/configuration.md from the settings model (a test checks it is current)
	$(UV_RUN) python -m memini.config_reference docs/configuration.md

eval: ## Retrieval eval, FakeEmbedder, against the committed baseline (MEMINI_DATABASE_URL, CREATEDB)
	$(UV_RUN) memini eval --fake-embeddings --baseline eval/code/baseline-fake.json $(EVAL_ARGS)

eval-qwen3: ## Retrieval eval, every mode, default model from local Ollama, against its baseline
	MEMINI_EMBEDDING_PROVIDER=openai_compatible $(UV_RUN) memini eval --modes all \
		--baseline eval/code/baseline-qwen3.json --margin $(QWEN3_MARGIN) $(EVAL_ARGS)

db: ## Start the development database (Postgres with pgvector) and wait until healthy
	docker compose up -d --wait db

db-down: ## Stop and remove the compose containers (database and service); the data volume stays
	docker compose down

smoke: ## M0 check: compose up (service and database), then MCP status from the host with a token
	scripts/compose-smoke.sh
