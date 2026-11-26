# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The Release workflow builds the desktop app from the tag and attaches it, unsigned, to the
  draft release with `desktop-SHA256SUMS`: `.dmg` for macOS (Apple silicon and Intel),
  `-setup.exe` for Windows x64, `.AppImage` and `.deb` for Linux x86_64. The release notes and
  [Desktop app](docs/install.md#desktop-app) say how to open it the first time.
- The app attached to a release installs that release's server: the desktop build waits for the
  published image, and the sidecar freeze (`desktop/sidecar/freeze.py --release-image`) pins it
  with its digest, and the tag's compose files, in the launcher it freezes. The build fails
  unless the frozen launcher names that image. Pull requests keep the repository's pin.
- One version for a release ([the rule](docs/releasing.md#the-rule-one-version)): the Release
  workflow fails, before PyPI and the draft release, unless the tag, the image, the Python
  package, the app's bundles and the image of every frozen launcher are of the same version
  (`scripts/check-release-versions.py`), the service in the image prints that version, and
  `tauri.conf.json` names no version of its own.

### Changed

- The app's build moved to the reusable workflow `desktop-app.yml`, which the Desktop workflow
  calls for a pull request and the Release workflow for a tag. Its CI artifacts are the bundles a
  release carries (`memini-desktop-<os>`) instead of an archive of the bundle folder.

### Changed

- The server launcher, and the desktop app that carries it, install and start the image of
  0.2.0-rc.4.

### Fixed

- The Windows installer's release check tries the install again for up to ten minutes: PyPI's
  index pages can lag its JSON API, and the run of 0.2.0-rc.4 failed there once.

## [0.2.0-rc.4] - 2026-11-26

### Added

- `GET /v1/workspaces/{workspace}/sources/{source}/streams` says what the index holds of each
  stream (`index`: records, chunks, the pinned commit and its committer time), whatever its last
  run did.
- `GET /v1/workspaces/{workspace}/sources/{source}/coverage`: how much of a repository the project
  map names: the projects with a service or a domain there, the share of its indexed files some
  domain names, when and by which token each project was last written, and the top-level folders
  of the files no domain names.
- Desktop app: one workspace switcher in the sidebar, with what a workspace is; Codebases, Data
  and Project map show the workspace chosen there.
- Desktop app, Codebases: each card shows what the index holds (files, chunks, commit and its
  date), the last check from the run's counts (`07:37 — no changes`), whether a running sync is a
  full listing or a diff, the folder of this computer it reads, and whether the project map names
  it, with the folders no domain names. The task for an agent is shown before it is copied, with
  the three steps to map the code bases.

### Changed

- Desktop app: the folders given to the local server that no code base reads are listed on the
  Server screen (**Folders on this computer**), not under the code bases of a workspace, which
  showed the folders of other workspaces. A card showed the last run's progress, 0% after a diff
  that changed nothing; it shows the state of the index instead.

- The server launcher, and the desktop app that carries it, install and start the image of
  0.2.0-rc.3.

### Fixed

- The Windows installer's release check answered its fake server's workspace list as
  `workspaces` instead of `items`, and it installed the just-published package before PyPI listed
  it; 0.2.0-rc.3 has no installers for that reason. The check now answers `items` and waits for
  PyPI to list the version, and the conventions test reads packaging code too.

## [0.2.0-rc.3] - 2026-11-26

### Fixed

- The release run's install test read the search answer's list as `hits`; since the API was
  unified it is `items`, and the run of 0.2.0-rc.2 failed there. A test now keeps the release
  scripts and workflows on the current answer keys. 0.2.0-rc.2 has no release; 0.2.0-rc.3 follows.

## [0.2.0-rc.2] - 2026-11-26

### Added

- A managed server rebuilds its search index by itself after an upgrade that changes how code
  is read (the setting `index_auto_rebuild`, which the server launcher sets), reusing every
  vector that did not change. Until a workspace is done, search answers `index_rebuilding` with
  how far it is, `GET /v1/status` and `memini status` show it, and the desktop app shows a bar
  on its Server and Codebases screens. [Upgrade the server](docs/install.md#upgrade-the-server).
- Mark a record sensitive instead of forgetting it: it stays stored and indexed, and every read
  leaves it out (search, code search, symbols, messages, meetings, threads, decisions supported
  only by it, the export) unless it asks with `include_sensitive` and a token with the new scope
  `sensitive`. No model reads a marked record. An admin marks or clears one with
  `PUT /v1/workspaces/{workspace}/records/sensitive` or `memini sensitive mark|unmark|list`, a push
  with `"sensitive": true`, and the desktop app on its Data screen. Scenario S19.
- See how far a git sync is while it runs: its run report carries `details.progress`, the files
  it writes, those read, chunked and embedded so far, and those committed, saved every few seconds
  (`GET /v1/runs/{id}`).
- Preview what a git source indexes before a sync spends time on it:
  `GET /v1/workspaces/{workspace}/sources/{source}/preview` counts the files of a ref that the
  source's rules, or rules to try, keep, by extension and by top-level folder.
- Limit how much of the computer the search model of a managed server uses while it indexes:
  `memini-server indexing set half|background|full`, half the cores by default. The desktop app
  sets it on the Server screen. [Limit the cores](docs/install.md#limit-the-cores-the-search-model-uses).
- The desktop app's Repositories tab is now **Codebases**: each code base is a card with a
  progress bar of its sync (files done of all, time left), its schedule with Pause and Resume, and
  what it indexes. Adding a code base leaves out tests, generated code, data files and assets by
  default, for the common languages; **What it indexes** previews what a sync keeps and saves a
  new scope. **Copy task for an agent**, there and on the Project map, gives a connected agent
  what to read and how to write the project map.
- Definitions and symbols in fifteen more languages: Java, C#, C, C++, PHP, Kotlin, Swift, Rust,
  Scala, Dart, Lua, shell, SQL, Objective-C and Elixir, twenty in all. Their files are chunked by
  function, method and type instead of line windows, `get_symbol` finds their definitions, and
  `search_code` and `get_symbol` filter by them (`languages`). A method added to a type outside
  it, in a Rust `impl`, a Swift or Dart `extension`, an Objective-C `@implementation`, a Kotlin
  extension function or a C++ `Dock::moor`, is named after the type.

### Changed

- One naming convention for every MCP tool, HTTP route and CLI command, with no aliases:
  [API conventions](docs/mcp/conventions.md). `tests/test_api_conventions.py` checks it. Renamed:
  - Tools: `read_source` is `get_file`, `add_document` is `put_document`; `get_symbol` is gone:
    `search_code` finds definitions in mode `symbol`, and by prefix in mode `symbol_prefix`.
  - Sources: the kind `git` is `repository` (migration 0023); arguments and fields name a source
    `source`/`sources`: `repository`, `repositories` and `unassigned_repositories` are gone.
  - One object by `id`: `get_thread`, `get_meeting`, `get_decision`, `supersede_decision`,
    `close_decision`, `retract_decision`, forget and the sensitive mark take `id`; naming a record by
    source and external id is gone, everywhere (evidence too: `record_id` and `anchor`); the
    answer of a pushed batch gives each record's `id` (`records`), and `memini push --json` lists
    them (`ids`).
  - Filters: `person` (`list_meetings`, `list_messages`), `person_ids` (`list_decisions`) and
    `author` (`list_commits`) are `persons`; `text` (`list_commits`) is `query`; `other_project`
    (`get_project`) is `linked_projects`; `include` and `exclude` of a source are
    `include_paths` and `exclude_paths`; the runs listing takes `sources`, `streams` and `cursor`;
    the persons listing takes `query` and `cursor` (were `q` and `after`), and `memini person
    list` takes `--cursor`.
    `list_meetings` and `list_messages` take `projects` and `domains`.
  - Answers: every list answers `items` (search hits, meetings, messages, commits, sources,
    streams, runs, tokens, persons, workspaces, sensitive records); `left_out` counts the results
    of records marked sensitive left out of search, `list_meetings`, `list_messages` and
    `list_decisions`; `next_until`, `next_before` and `next_after` are `next_cursor`.
  - Times: a decision's `decided_at` is `occurred_at` and `recorded_at` is `created_at`; a
    message's `sent_at` is `occurred_at`; a hit's `at` is `chunk_occurred_at`; a mark's
    `sensitive_at` is `marked_at`. A hit's chunk id is `id`, a record's and a person's own id `id`.
  - HTTP: `GET /v1/symbols` and the routes that read a thread or a meeting by external id are
    gone; `PUT …/sources/{source}/documents` writes a document; forget is
    `POST /v1/workspaces/{workspace}/records/{id}/forget` and the mark
    `PUT /v1/workspaces/{workspace}/records/{id}/sensitive`.
  - CLI: every MCP read tool is a command of the same name with its arguments as options:
    `memini search`, `search-code`, `get-file`, `list-commits`, `list-messages`, `get-thread`,
    `list-meetings`, `get-meeting`, `list-decisions`, `get-decision`, `get-catalog`,
    `catalog-guide`, `get-project`, `get-domain` and `status`. A list filter keeps its plural
    name and repeats (`--persons me --persons bo@example.test`), and the topic of a list is
    `--query`. `memini search --code` is `memini search-code`; `memini meetings` and
    `memini messages` are `memini list-meetings` and `memini list-messages`; `--workspace`,
    `--source`, `--person`, `--role`, `--kind`, `--path` and `--language` of those commands are
    `--workspaces`, `--sources`, `--persons`, `--roles`, `--kinds`, `--paths` and `--languages`.
    Admin commands stay grouped by noun: `memini forget <workspace> <id>`, `memini sensitive
    mark|unmark <workspace> <id>`, `memini decision record --occurred-at` with `--evidence
    RECORD_ID[#ANCHOR]`, `memini runs` with `--cursor`.
- `get_file` refuses a file whose record is marked sensitive unless asked with
  `include_sensitive`, as `get_thread` does; reading a marked record answers 403
  `sensitive_record`.
- A release writes its version with `scripts/bump-version.py`, which also moves the changelog
  entries; the release run refuses a tag whose desktop app version differs.
  [Releasing](docs/releasing.md).
- The server launcher, and the desktop app that carries it, install and start the image of
  0.2.0-rc.1, so a managed server has the catalog, domains and the project map. A managed server
  of 0.1.0 moves to it at its next start, and its database is migrated on the way.
- The chunker is version 3, which changes the index fingerprint: after the upgrade, search refuses
  a workspace until `memini reindex --workspace NAME` has rebuilt it. Vectors are reused, so only
  the files of the new languages are embedded again.
- On a Mac the search model runs in Ollama on the Mac, about a hundred times faster than in
  Docker, which has no GPU there. The desktop app's first setup finds Ollama, downloads the model
  with its progress shown and installs the server in this mode, or offers to install Ollama with
  Homebrew; the Server screen moves an installed server's model to the Mac or back. The launcher
  does it with `memini-server embedding host|container|status` and
  `memini-server start --embedding host`; the index stays as it is.
  [Run the search model in Ollama on a Mac](docs/install.md#run-the-search-model-in-ollama-on-a-mac).

### Fixed

- A first git sync that is stopped, by an edit of the source or a restart, keeps the files it has
  written: it commits every 30 seconds of writing instead of once per batch of a hundred files,
  which without a GPU took over half an hour and was lost whole.
- The desktop app's window no longer scrolls as a whole: a selection dragged past the edge or a
  focused element moved the side bar and the status bar out of the window. Only the screen
  scrolls.
- The Repositories screen opens the form to add a repository, and the one to create a workspace,
  on a click, instead of showing them all the time.
- The first setup in the desktop app says that Docker is downloading the application, the
  database and the search model (about 2 GB) while no service exists yet, instead of pointing to
  an empty list of services for minutes.
- A folder whose path holds `$` is mounted at that path: the launcher writes it as `$$`, which
  Compose reads as one `$`.

## [0.2.0-rc.1] - 2026-11-25

### Added

- A desktop app for macOS, Windows and Linux. It sets up a local server in one step with the
  launcher inside the app (no Homebrew, Scoop or Python needed), starts, stops and restarts it,
  switches MCP on and off, issues tokens, and connects or disconnects agent clients (Claude Code,
  Cursor, Codex, VS Code, Gemini CLI, Windsurf, Zed, Cline, OpenCode) in one click. It shows the
  project map and edits it, shows what the base holds, and runs the read tools from a form. It
  also connects to a remote server by address and token, and checks a connection step by step.
  Tokens stay in the operating system's keychain. The app is not signed yet.
- `search` with `projects` or `domains` also returns the threads, meetings and documents that
  belong to them: by what they were pushed with, a default of their source or stream, or the
  domains the model gave their decisions.
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
- Remove a managed server with `memini-server uninstall --yes`: its containers, its data and its
  state directory; Docker keeps the images. The desktop app has a Remove button for it, which
  also forgets the connection to that server and its token.
  [Remove the server](docs/install.md#remove-the-server).
- Add repositories without a terminal. The desktop app's Repositories screen adds a folder of this
  computer, or a remote by its address, as a git source of a workspace (and creates the workspace),
  syncs it, and shows each branch's state. The Server screen's Git access gives the server an ssh
  key, with the fingerprints of its hosts to compare, or an https token. Underneath,
  `memini-server repos add|remove|list` mounts folders read-only into a managed server, and
  `memini-server git ssh-key|https-token|remove|status` sets its access, the secret on stdin.
  [Repositories and git access](docs/install.md#repositories-and-git-access).
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

[Unreleased]: https://github.com/npavlov/memini/compare/v0.2.0-rc.4...main
[0.2.0-rc.4]: https://github.com/npavlov/memini/releases/tag/v0.2.0-rc.4
[0.2.0-rc.3]: https://github.com/npavlov/memini/releases/tag/v0.2.0-rc.3
[0.2.0-rc.2]: https://github.com/npavlov/memini/releases/tag/v0.2.0-rc.2
[0.2.0-rc.1]: https://github.com/npavlov/memini/releases/tag/v0.2.0-rc.1
[0.1.0]: https://github.com/npavlov/memini/releases/tag/v0.1.0
[0.1.0-rc.1]: https://github.com/npavlov/memini/releases/tag/v0.1.0-rc.1
