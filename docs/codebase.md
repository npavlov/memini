# Developer guide

Memini runs one Python service and a PostgreSQL database with pgvector. HTTP and MCP share the same query and decision services. The CLI can call HTTP remotely or run operator commands next to the database. Start at the boundary where you observe the problem, then follow the relevant path below.

| Starting point | Where to look |
|---|---|
| A CLI option, exit code or HTTP request | [CLI entry point and command registration](../src/memini/cli/__init__.py), [HTTP client](../src/memini/cli/client.py), [CLI tests with a mock service](../tests/test_cli_client.py) |
| An HTTP route or MCP tool | [FastAPI application factory](../src/memini/api/app.py), [MCP server](../src/memini/mcp/server.py), [tool contracts and examples](mcp/README.md) |
| A scheduled sync or background job | [Handler wiring](../src/memini/cli/serve.py), [scheduler](../src/memini/jobs/scheduler.py), [queue](../src/memini/jobs/queue.py), [worker](../src/memini/jobs/worker.py), [failure and recovery tests](../tests/test_failures_db.py) |
| A schema change or stored value | [SQL migrations](../src/memini/db/migrations), [migration runner](../src/memini/db/migrate.py), [schema constraints and scope tests](../tests/test_schema.py) |

## Trace a push from the CLI to the index

1. `memini push` reads records or batches and sends them to the source's records route. A batch carries whole records. A second version of a thread replaces its messages as a unit. The route validates the wire shape and checks the token's ingest scope. [CLI](../src/memini/cli/push.py) · [HTTP route](../src/memini/api/ingest.py) · [sample inputs](intake.md#records)
2. Batch preparation validates payloads and redacts secrets. The record ID derives from source ID, kind and external ID, so a record keeps its identity when another stream of the same source encounters it. The batch writer resolves source access, applies filters, upserts records and reconciles eligible snapshots. A complete window advances its checkpoint in the data transaction. An incomplete window can store partial records while retaining the previous checkpoint. [Validation](../src/memini/ingest/validate.py) · [ID contract](../src/memini/ids.py) · [batch writer](../src/memini/ingest/store.py) · [HTTP retry and scope tests](../tests/test_api_ingest_db.py)
3. With embeddings enabled, the payload indexer calls the chunker and index writer in the batch transaction. The writer reuses vectors for identical text under the same embedding key, preserves anchors and refuses a stale workspace fingerprint. A push without an embedding provider can store raw data for a later rebuild. [Payload indexer](../src/memini/ingest/indexing.py) · [index writer](../src/memini/index/writer.py) · [fingerprint](../src/memini/index/fingerprint.py) · [rebuild tests](../tests/test_index_rebuild_db.py)

**If a late reply disappears**, first inspect the submitted whole thread, then its ID and window, then chunk anchors. **If raw text exists but search cannot find it**, check whether the batch indexed it, whether the fingerprint matches and whether the query token can see the workspace.

## Trace a Git or MCP sync

A Git sync fetches into a bare mirror, compares the new commit with the stream's pinned commit and indexes selected files in batches. A failure after a committed batch leaves the previous pin in place and marks the sync partial. A retry reconciles the ref. A search locator identifies the commit and line range to read. [Sync and transaction boundaries](../src/memini/gitsync/sync.py) · [chunker](../src/memini/chunking/chunker.py) · [partial sync, retry and reindex tests](../tests/test_gitsync_db.py)

MCP intake uses a recipe to plan missing calls. The service executor or agent session collects raw responses. Parsing and completeness checks run on the service, allowing a parser fix to replay stored responses. Look at a window's missing calls before changing its checkpoint logic. [Executor](../src/memini/executors/core.py) · [agent session](../src/memini/executors/agent/session.py) · [parser](../src/memini/recipes/parsing.py) · [replay](../src/memini/responses/replay.py) · [executor parity tests](../tests/test_m3_check_db.py)

## Trace a search or journal change

- **Query filters and ranking.** HTTP routes and MCP tools call the same query functions, which validate arguments and pass the read filter through to retrieval. General search also selects active journal items. Change a public filter in the adapters and query model, then exercise both transports and workspace isolation. [HTTP](../src/memini/api/query.py) · [MCP](../src/memini/mcp/query.py) · [query model](../src/memini/query/model.py) · [shared service](../src/memini/query/service.py) · [retrieval](../src/memini/search/engine.py) · [end-to-end tests](../tests/test_query_db.py)
- **Catalog.** Projects, services and domains are authored rows, written whole by `put_project` and compared entry by entry, so the same document again writes nothing. No chunk holds a project or a domain: the `projects`, `domains` and `services` filters and a result's `context` are computed from the path masks when a question is asked, by one matcher that Postgres and Python both evaluate. Change a mask rule in the pattern functions and run the test that asks both about the same paths. [Catalog service](../src/memini/catalog/service.py) · [filters and context](../src/memini/search/catalog.py) · [pattern functions](../src/memini/search/patterns.py) · [tables](../src/memini/db/migrations/0018_catalog.sql) · [end-to-end tests](../tests/test_catalog_db.py)
- **Manual decisions.** API and MCP writes take a statement and evidence through the decision service to the append-only journal. Superseding, closing or retracting retires an active item. History retains its identity, evidence and links. The database enforces these rules even with concurrent writers. [Decision service](../src/memini/decisions/service.py) · [journal writes](../src/memini/knowledge/journal.py) · [database constraints](../src/memini/db/migrations/0008_knowledge.sql) · [journal tests](../tests/test_knowledge_db.py)
- **Extracted decisions.** Changed threads and meetings can enqueue extraction work. The job checks content and extractor identity, gates model access by workspace and source policy, validates evidence anchors, and checks for duplicates and supersession before it writes. A rerun must preserve manual decisions and avoid duplicate items. For a wrong replacement, inspect the evidence, candidates and verdict before changing the journal. [Job](../src/memini/extraction/job.py) · [provider gate](../src/memini/llm/gate.py) · [reversal test](../tests/test_supersession_db.py) · [re-extraction tests](../tests/test_reextract_db.py) · [decision eval](../eval/decisions/README.md)

## Change storage and authorization together

Check the operation's scope at its route or tool. Authorized reads carry a `ReadFilter` into the SQL query. Writes carry `WriteAccess` when locating mutable rows. Workspace-scoped tokens must see the same permitted rows through HTTP, MCP and searches across sources. Test the allowed and denied workspace with actual rows. [Authorization predicates](../src/memini/auth/authorization.py) · [HTTP dependencies](../src/memini/api/auth.py) · [access tests](../tests/test_auth_db.py)

Add the next consecutive `NNNN_name.sql` file for a schema change. The runner serializes migrations with an advisory lock and commits each migration with its checksum. Editing an applied file makes the runner refuse the database. Exercise the schema against an empty database and, for changes to existing rows, an upgrade with earlier data. [Migration tests](../tests/test_db.py) · [release upgrade check](../scripts/test-upgrade.sh)

## Verify a change

Run from the checkout after [development setup](../CONTRIBUTING.md#set-up). A quick path through the contracts above is:

```sh
uv run pytest -q tests/test_ingest.py tests/test_index_fingerprint.py tests/test_migration_files.py
uv run pytest -q tests/test_api_ingest_db.py::test_a_push_source_takes_a_batch_and_the_same_batch_again_changes_nothing
uv run pytest -q tests/test_query_db.py::test_http_search_routes_mirror_the_tools
uv run pytest -q tests/test_supersession_db.py::test_thread_b_supersedes_thread_a_and_the_restatement_is_a_duplicate
```

The last three commands need PostgreSQL with pgvector. Start the development database with `make db` or set `MEMINI_TEST_DATABASE_URL` to a test server whose role can create disposable databases. Tests create and drop their own databases. For a merge, run `make check` and `make test-integration`. For retrieval or prompt changes, also compare the relevant [retrieval evaluation](../Makefile) or [decision evaluation](../eval/decisions/README.md). Use synthetic fixtures. [Contribution checks](../CONTRIBUTING.md#test)

## Implemented and planned

The current tree includes Git indexing, push and recipe-based intake, search, identity resolution, manual and extracted decisions, and one principal with workspace-scoped tokens. The [bundled recipe directory](../src/memini/recipes/bundled) contains no server-specific TOML in this version. [Hearth](../tests/fixtures/recipes/hearth.toml) is an executable example with a test parser. Multi-user membership and per-user permissions, native mail connectors and a compiled wiki are roadmap items. Use the [requirements](requirements.md#11-later) for scope, the [milestone plan](plan.md) for acceptance scenarios and Beads for current work.
