# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Requirements, implementation plan and prior art documents.
- Apache-2.0 license.
- Community files: contributing guide, security policy, code of conduct and this changelog.
- Python project skeleton managed with uv: `make lint`, `make typecheck`, `make test` and `make check`.
- Development database in `compose.yaml`: Postgres 18 with pgvector 0.8.7 and pg_trgm, on
  `127.0.0.1:55432`.
- Typed configuration from `MEMINI_*` environment variables and an optional TOML file
  (`MEMINI_CONFIG_FILE`); secrets from the environment or a `*_FILE` path, never in logs or errors.
- Database access with psycopg 3 (async, plain SQL) and forward-only SQL migrations with a concurrency-safe
  runner; the first migration creates the `vector` and `pg_trgm` extensions.
- Core schema: workspaces, principals, tokens, sources, streams, records, persons, aliases and runs;
  deterministic record ids checked by the database; cross-workspace references refused.
- HTTP API skeleton (FastAPI): `/healthz`, `/readyz` (checks the database), `/version`, `/openapi.json`;
  one error body for every error, without echoing request input; `X-Request-ID` on every response.
- Bearer tokens (`memini_…`, stored as SHA-256 digests, shown once) with `read`, `ingest` and `admin`
  scopes; one authorization filter for every read; `GET /v1/workspaces`.
- Job queue in Postgres: leases, retries with backoff, quarantine, one job per source at a time,
  per-stream schedules and graceful shutdown.
- JSON logs with request and run ids that mask tokens and secrets; a run report (steps, durations,
  counts, LLM tokens) for every job attempt.
- MCP endpoint at `/mcp` (streamable HTTP, stateless, JSON), behind the same bearer tokens, with the
  `status` tool: version, schema, readable workspaces, stream lag and last run, and whether the base
  is current.
- `memini` command. Client commands call the running service over HTTP with `MEMINI_URL` and a token
  (`MEMINI_TOKEN` or `MEMINI_TOKEN_FILE`): `workspace`, `source`, `streams`, `sync [--wait]`, `runs`,
  `run`, `token`, `search`. Operator commands work on the database inside the container: `serve`
  (HTTP API, MCP, job worker and scheduler; JSON logs), `migrate`, `bootstrap` (the first admin token),
  `reindex`, `eval`, `credentials`, `workspace llm`. Token administration over HTTP: `/v1/tokens`.
- Service image (non-root, read-only root filesystem, healthcheck on `/readyz`) and a compose stack
  that runs the service and the database, published on localhost only; `make smoke` runs the M0 check.
- Embedding providers (an OpenAI-compatible endpoint; an in-process ONNX model with the optional
  `local-embeddings` extra, files pinned by digest) and a per-workspace index fingerprint that refuses
  search on a mismatched index and names `memini reindex`.
- Tree-sitter code chunker for Python, TypeScript/JavaScript, Ruby and Go, with a symbol table and
  line windows for other files; chunk ids stable across runs and unaffected by edits elsewhere.
- Git mirror manager: one bare mirror per source, fetched atomically (a failed fetch changes nothing),
  hardened git invocations, https and ssh by default; the image ships git and a mirror volume.
- Index writer: chunk and symbol tables with HNSW, full-text and trigram indexes; unchanged chunks cause
  no embedding call and no row change; `memini reindex --workspace NAME` rebuilds from raw.
- Source administration over HTTP: create workspaces, add, edit and delete git sources with tracked refs
  and a schedule, list streams, trigger a sync, list runs and read run reports.
- Incremental git sync: a pinned commit per tracked ref, only changed files read and embedded, deleted
  and skipped files tombstoned, the pin advanced only when the run succeeds; `memini reindex` reads code
  from the mirrors.
- Search library: hybrid (dense and full-text with reciprocal rank fusion), dense, lexical, exact, regex
  (under a statement timeout) and symbol modes; filters by workspace, source, repository, kind,
  language, path glob and time; code hits carry `repo@sha:path#lines`; a stale index is refused.
- Markdown files in repositories (`.md`, `.markdown`, `.mdx`) are indexed as document records, chunked
  by heading, and searchable beside code. The chunker version changed: run `memini reindex` on
  existing workspaces.
- Query tools over MCP and HTTP: `search`, `search_code` (semantic, exact, regex, symbol), `get_symbol`
  and `read_source` (from the mirror at the pinned commit, or a given ref or commit; path traversal
  refused).
- Retrieval eval: `memini eval` runs a 101-question suite (English and Russian, code, documents,
  symbols) over synthetic repositories in a scratch database, reports recall@k and MRR per mode, kind
  and language, and fails when a metric drops below the baseline.
- Git sync: a ref deleted at the remote is marked missing and its files tombstoned instead of failing;
  `builtin_excludes` can be switched off per source; each file gets the time of the last commit that
  changed it; reindex reads repository files in batches (about 5x faster).
- Ingest API: `POST /v1/workspaces/{ws}/sources/{src}/records` takes record batches for push sources,
  with window claims that move the checkpoint in the same transaction, tombstones, snapshot reconcile
  and size limits; a resent batch changes nothing.
- Credential store for sources: AES-256-GCM with a key from the environment, crash-safe OAuth refresh,
  `memini credentials set|list|delete|rotate-key`.
- LLM provider layer: a local and a cloud OpenAI-compatible slot behind one `llm_policy` gate; content of
  a source whose policy is `local` or `none` never reaches a cloud model; `memini workspace llm`.
- Knowledge journal: decisions and open questions with evidence and supersession links, append-only and
  enforced by the database.
- Qwen3-Embedding 0.6B served by Ollama is the default embedding model; setting
  `MEMINI_EMBEDDING_PROVIDER=openai_compatible` is enough (`.env.example`).
- Redaction at ingest: keys, tokens, private keys, credentials in URLs and Luhn-valid card numbers are
  replaced before anything is stored, in payloads, normalized text, chunks and `read_source` output.
- Threads, meetings and pushed documents are chunked (message groups, summary sections, transcript
  windows, headings) and indexed on ingest.
- Full-text search with Russian and English stemming and code identifiers split into words; hybrid
  fusion weights the dense ranks (open question 1).
- Intake planner for MCP sources: forward windows with an overlap, an initial load and history backfill
  down to the retention start.
- Per-token rate limits, a request body limit and a time limit on every query a request runs.
- Configuration reference generated from the settings model (`docs/configuration.md`).
- Backup and restore scripts and procedure (`docs/operations/backup.md`), with a test that a restored
  database answers searches identically.
- Search hits of messages and meetings carry an anchor and a link into the record.
- CI on GitHub Actions: lint, type check, unit and integration tests on Python 3.12 and 3.13; a smoke job
  builds the image and runs the compose stack end to end; the retrieval eval
  gates pull requests (fake embeddings), and a nightly workflow runs it with qwen3 in Ollama.

[Unreleased]: https://github.com/npavlov/memini/commits/main
