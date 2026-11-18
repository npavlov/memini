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
- `memini` command: `serve` (HTTP API, MCP, job worker and scheduler; JSON logs), `migrate`,
  `workspace create|list`, `token create|list|revoke`.
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
- CI on GitHub Actions: lint, type check, unit and integration tests on Python 3.12 and 3.13; a smoke job
  builds the image and runs the compose stack end to end.

[Unreleased]: https://github.com/npavlov/memini/commits/main
