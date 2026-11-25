# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Get the first admin token of a managed server from the launcher: `memini-server bootstrap`
  prints it, and with `--json` hands it to a program as one object. The desktop app uses it to
  set up a local server in one step, and carries the launcher itself, frozen as a sidecar.
  [Output for programs](docs/install.md#output-for-programs).
- See who changed what with `list_commits` over MCP and `GET /v1/commits`: commits by
  repository, path, author, period and message text, newest first, each with its author, the
  person the author resolves to and the changed paths. History is read from the repository
  mirror on demand and never stored. [Tool reference](docs/mcp/list_commits.md).
- Switch the MCP endpoint off with the setting `mcp_enabled` (`MEMINI_MCP_ENABLED`), or with
  `memini-server mcp off` on a managed server. While it is off, `/mcp` answers 404 `mcp_disabled`
  and the HTTP API keeps working. `GET /v1/status` and `memini status` say which it is.
- Drive the server launcher from a program: `memini-server status`, `start`, `stop` and `mcp`
  print one JSON object with `--json`. [Output for programs](docs/install.md#output-for-programs).
- A catalog of what code belongs to. A project has services (a repository, or one directory of
  it) and domains with subdomains that name their code by path masks. An agent writes a project
  as one document with `put_project`, following `catalog_guide`; `get_catalog` and `get_domain`
  read it. `search` and `search_code` take `projects`, `domains` and `services`, and every code
  hit, definition and `read_source` answer says what its file belongs to (`context`). Changing
  the catalog needs no reindex. [MCP tool reference](docs/mcp/README.md#tool-reference).
- Declare how projects are connected: a service of the catalog lists the services it uses, in
  its own or another project of the workspace, each with a kind (`library`, `http`, `queue`,
  `other`), a note and the places in its code that show the use, as part of the `put_project`
  document. `get_project` and `GET /v1/workspaces/{workspace}/projects/{project}` return a
  project with its links in both directions and a filter by the other project; `get_catalog`
  shows them too. [Tool reference](docs/mcp/get_project.md).
- Find decisions by project and domain. A decision takes them from the records its evidence is
  in (named by whoever pushed them, or a default of the source or stream), from a link by hand,
  from code evidence matched against the domain masks, or last from the `assign_domains` model,
  and carries them as `context`. `list_decisions` and the decisions of `search` filter by
  `projects` and `domains`, a domain with its subdomains; `get_domain` lists a domain's active
  decisions. [Tool reference](docs/mcp/list_decisions.md).

## [0.1.0] - 2026-11-22

### Fixed

- Keep finished work, failure causes, vendor facts, file locations and questions about a call
  out of the decision journal. Records are extracted again under `decisions-v6`.
- Restore multilingual search ranking with the default local reranker on Linux x86-64 and arm64. Existing installations can reuse their verified model cache and search index.

### Known limits

- Mail import takes normalized threads from an exporter or an agent connected to the mailbox.
  Native mail connectors are planned for a later release. [Mail thread example](docs/examples/mail-thread.json).
- Decision extraction is measured on synthetic records only. With `qwen3:4b-instruct` it found
  all 24 labelled items with no false entries, also with an instruction added to each record,
  and 29 of 30 supersession cases. Review decision evidence before relying on extracted items.
  [Decision eval](eval/decisions/README.md).
- Items extracted by 0.1.0-rc.1 stay in the journal after an upgrade. Run
  `memini extract WORKSPACE` to read existing threads and meetings with the new extractor; the
  journal is append-only, so review or retract earlier false entries by hand.
- A database backup retains records forgotten after the backup. Restoring it requires the
  encryption keys from the time of the backup. [Restore procedure](docs/operations/backup.md).

## [0.1.0-rc.1] - 2026-11-21

### Added

- Export workspace records and decisions as JSONL with `memini export --workspace NAME`.
  Use `--include-private` to include records from private sources.
- Install a tagged GHCR image with a containerized embedding provider using the
  [Docker-only quickstart](docs/install.md#install-a-published-image-with-docker-only).

- Read the decision journal over MCP and HTTP, search active decisions alongside records, and
  follow an item's evidence and supersession history. [MCP tool reference](docs/mcp/README.md).
- Follow the [operations runbook](docs/operations/runbook.md) for status, recovery, upgrades,
  index rebuilds, forgetting a record and rotating the credential encryption key.

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
- Stream health: `GET /v1/status`, `memini status` and the `status` tool show each stream's last result
  and error line, failures, lag, how far history is loaded, unindexed records and stale indexes.
- `memini decision record|supersede|close|retract`; an MCP call of a write tool counts against the write
  rate budget; compose passes the request-limit settings through.
- Synthetic comms fixtures (`eval/comms/`): chat, mail and meetings of a fictional company, with answer
  keys for M2 queries and labels for the decision eval.
- `memini forget` deletes a record and everything derived from it, with an audit entry, and keeps it from
  coming back; per-source retention removes expired records on a schedule.
- Source filters for push sources (allowed channels and domains, excluded authors, titles, automated mail)
  and `memini source prune`.
- Persons: resolution by e-mail and chat id at ingest, aliases, reversible merge and split, "me";
  `memini person ...`.
- Raw MCP responses are stored (redacted) per window; `memini replay` re-runs a parser over them without
  calling the source.
- Compose passes every service setting from an optional `.env.memini` (`.env.memini.example`) and mounts
  keys, tokens and the ssh key as compose secrets, with their paths in `.env`; needs Docker Compose
  2.24.0. Embedding, log-level and request-limit settings move from `.env` to `.env.memini`, with
  `MEMINI_EMBEDDING_URL=http://host.docker.internal:11434/v1` next to the provider.
- Parser interface and completeness checks for MCP recipes: an incomplete window keeps its checkpoint and
  names the calls that would complete it.
- Observability: `GET /metrics` (Prometheus format, its own bearer token, no names or ids in labels), a
  slow-statement log without parameters, and the freshness gate (`memini status --gate`).
- Decision eval: `memini eval decisions` scores an extractor on labelled synthetic threads and meetings
  (English and Russian, reversals, duplicates, traps): extraction precision and recall, supersession
  accuracy; a keyword stub sets the first baseline.
- Comms tools over MCP and HTTP: `list_meetings` (default: meetings I attended), `get_meeting`,
  `list_messages` (to me, mentioning me, or from a person), `get_thread`, `add_document`; search filters by
  person and role.
- Optional reranking with Qwen3-Reranker 0.6B in the process (`MEMINI_SEARCH_RERANKER=local`, needs the
  `local-embeddings` extra): on the code suite hybrid R@1 rises from 0.61 to 0.78 for about 0.5 s per
  search; off by default.
- Agent executor for MCP sources reachable only inside an agent session: a run API
  (`…/agent-runs`: start, next, responses, finish) and a Claude Code plugin with the `memini-intake`
  skill and a capture hook (`integrations/claude-code`); the agent sees a receipt, not the payload.
- Service executor (`mcp_sync`): MCP recipes run headlessly over streamable HTTP or stdio, with an egress
  allowlist, internal-address refusal, a rate budget and retries; mcp source admin in the API and CLI.
- Decision extraction: with an LLM provider configured, new or changed threads and meetings get an
  `extract` job (record text isolated as data, items need a valid anchor, supersession checked against
  similar active items); `memini eval decisions --extractor llm` measures a model.
- Install guide (`docs/install.md`): from a clone to a search over a repository and a pushed chat thread
  with compose, Claude Code connected, and the errors a newcomer meets; `bin/memini` runs the CLI inside
  the service's container, so the host needs Docker only; a synthetic example batch in `docs/examples/`.
- `memini sync --wait` says when a sync is still queued after 30 s, and that a git source syncs only
  while an embedding provider is set.
- `memini extract` and `POST /v1/workspaces/{workspace}/extract`: enqueue decision extraction of stored
  records the current extractor has not read (backfill after configuring a provider, re-run after a new
  prompt or model); a new reading no longer brings back replaced, answered or retracted items.
- Failure tests: a worker killed mid-sync, the database crashed mid-batch, MCP and git sources that
  hang, with a reusable consistency check; `make test-integration` starts a throwaway Postgres container
  for them (they skip without Docker).
- `memini push`: chat and mail threads, meetings, documents and tombstones from a JSON or JSON-lines file
  or stdin into a push source; each record is checked first, bad ones are reported while the rest are
  stored, batches stay under the service limits, and 429/503 are retried. `docs/intake.md` has the record
  shapes and the agent rules; the `memini-push` skill is for agents.
- The service image reranks by default: the reranker's runtime and pinned model files are baked in
  (checked by SHA-256 at build and by digest at start; no download at run time), depth 3;
  `MEMINI_SEARCH_RERANKER=none` turns it off; `search_reranker_threads` caps its threads.
- `memini bench` (`make bench`): indexing time, embedding throughput, sizes, HNSW recall and search
  latency by mode over a repository and a generated message set; numbers, targets and the smallest
  supported machine in `docs/operations/performance.md`.
- Supersession candidates by statement vectors fused with words (`extraction_candidate_retriever`, default
  `hybrid`); vectors kept per item (migration 0017). Check verdicts `same`/`replaced`/`answered`, and no
  duplicate when the values differ: supersession accuracy 0.96 with `qwen3:4b-instruct` on the decision
  eval. `memini eval decisions --candidates/--candidate-limit`.
- Prompt-injection hardening: extraction prompt `decisions-v3` (escaped data block, an `instruction`
  label that never becomes an item); `data_notice` on every result holding source text; fenced text
  content nothing can close early; the injection corpus `eval/injection` and `memini eval decisions
  --inject`.
- CI on GitHub Actions: lint, type check, unit and integration tests on Python 3.12 and 3.13; a smoke job
  builds the image and runs the compose stack end to end; the retrieval eval
  gates pull requests (fake embeddings), and a nightly workflow runs it with qwen3 in Ollama.


### Fixed

- Search no longer runs value-blind cached plans: each statement is planned for its own values (hybrid
  p95 1.2 s → 0.11 s at 77,000 chunks).
- onnxruntime no longer sends usage telemetry (`ORT_DISABLE_TELEMETRY` is set before it loads), for
  local embeddings and the reranker.
- A model run uses no more threads than the container's CPU limit: one thread per host core made
  reranking 11 times slower in a 2-CPU container.
- The HTTP API answers 503 `unavailable`, not 500, while the database is down or restarting.
- A stalled MCP server's handshake is reported as `ConnectError`, not an exception group.

### Security

- Restrict Git network egress through an operator-managed proxy and constrain stdio MCP server
  invocations to exact arguments. [Deployment controls](docs/security.md).

- A secret scan before each commit (`make hooks`) and over the whole history in CI (gitleaks, pinned and
  checksum-verified); fixture directories must carry a `SYNTHETIC.md` marker.

### Known limits

- Mail import takes normalized threads from an exporter or an agent connected to the mailbox.
  Native mail connectors are planned for a later release. [Mail thread example](docs/examples/mail-thread.json).
- On 52 synthetic records the 4B decision extractor's precision fell from 0.667 to 0.615 when
  an instruction was added to each record, beyond the target tolerance of 0.02. Recall was
  1.00 in both runs. Review decision evidence before relying on extracted items.
- Local decision extraction quality depends on the model and prompt. The synthetic decision
  suite reports precision, recall and supersession accuracy. [Decision eval](eval/decisions/README.md).
- A database backup retains records forgotten after the backup. Restoring it requires the
  encryption keys from the time of the backup. [Restore procedure](docs/operations/backup.md).

[Unreleased]: https://github.com/npavlov/memini/compare/v0.1.0...main
[0.1.0]: https://github.com/npavlov/memini/releases/tag/v0.1.0
[0.1.0-rc.1]: https://github.com/npavlov/memini/releases/tag/v0.1.0-rc.1
