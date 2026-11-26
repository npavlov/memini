SHELL := /bin/bash
.DEFAULT_GOAL := help

# `uv run` syncs the environment from uv.lock before each command.
UV_RUN := uv run

# Extra arguments of `memini eval` for the eval targets, e.g. EVAL_ARGS="--json report.json".
EVAL_ARGS ?=
# Extra arguments of `memini eval decisions` for eval-decisions, e.g. "--extractor oracle".
DECISION_EVAL_ARGS ?=
# Extra arguments of `memini bench` for the bench target, e.g. BENCH_ARGS="--json bench.json".
BENCH_ARGS ?=
# The margin of eval-qwen3. Hybrid results move by up to about 0.03 from run to run until tied
# lexical results are ordered by something stable across runs (docs/requirements.md, section 10).
QWEN3_MARGIN ?= 0.05
# Worker processes of make test-integration (pytest-xdist): auto is one per CPU, 0 runs them in
# this process. Each test has a database of its own, so workers share the server, not data.
# --dist loadfile keeps the tests of one file on one worker: a module's fixtures (such as the
# Postgres container of test_failures_db.py) start once, and a file's tests run in their order.
INTEGRATION_WORKERS ?= auto

.PHONY: help sync lint format typecheck test test-integration check eval eval-qwen3 eval-decisions db db-down smoke

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
	$(UV_RUN) pytest -m integration -n $(INTEGRATION_WORKERS) --dist loadfile

.PHONY: backup-test
backup-test: ## Dump a filled test database, restore it into an empty one and compare (Postgres as above)
	$(UV_RUN) pytest -m integration tests/test_backup_db.py

check: lint typecheck test ## Everything a pull request must pass

.PHONY: hooks secret-scan
hooks: ## Use the versioned git hooks (.beads/hooks): a secret scan before each commit; fetch gitleaks
	@previous="$$(git config --get core.hooksPath || true)"; \
	if [ -n "$$previous" ] && [ "$$previous" != .beads/hooks ]; then \
		echo "core.hooksPath was $$previous"; \
	fi
	git config core.hooksPath .beads/hooks
	scripts/secret-scan.sh install

secret-scan: ## Scan every commit of HEAD for secrets (pinned gitleaks, rules in .gitleaks.toml)
	scripts/secret-scan.sh history

.PHONY: config-docs
config-docs: ## Regenerate docs/configuration.md from the settings model (a test checks it is current)
	$(UV_RUN) python -m memini.config_reference docs/configuration.md

eval: ## Retrieval eval, FakeEmbedder, against the committed baseline (MEMINI_DATABASE_URL, CREATEDB)
	$(UV_RUN) memini eval --fake-embeddings --baseline eval/code/baseline-fake.json $(EVAL_ARGS)

# eval-qwen3 searches as the image does, with the reranker on: it needs the extra local-embeddings,
# which uv installs, and the reranker's files, fetched on the first run into ~/.cache/memini/models
# (or MEMINI_SEARCH_RERANKER_MODEL_DIR) and verified against their digest.
eval-qwen3: ## Retrieval eval, every mode, default model from local Ollama, reranker on, against its baseline
	MEMINI_EMBEDDING_PROVIDER=openai_compatible $(UV_RUN) --extra local-embeddings memini eval \
		--modes all --reranker local \
		--baseline eval/code/baseline-qwen3.json --margin $(QWEN3_MARGIN) $(EVAL_ARGS)

eval-decisions: ## Decision eval, keyword stub, against the committed baseline (MEMINI_DATABASE_URL set; the stubs connect to nothing)
	$(UV_RUN) memini eval decisions --baseline eval/decisions/baseline-keyword.json $(DECISION_EVAL_ARGS)

.PHONY: bench
bench: ## Performance benchmark in a scratch database (MEMINI_DATABASE_URL, CREATEDB; ~25 min; docs/operations/performance.md)
	$(UV_RUN) memini bench $(BENCH_ARGS)

db: ## Start the development database (Postgres with pgvector) and wait until healthy
	docker compose up -d --wait db

db-down: ## Stop and remove the compose containers (database and service); the data volume stays
	docker compose down

smoke: ## M0 check: compose up (service and database), then MCP status from the host with a token
	scripts/compose-smoke.sh
