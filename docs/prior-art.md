# Prior art

Snapshot of a web survey made on 2026-11-18. Not every fact about third-party projects was verified on a
primary source. Re-check before relying on one.

## No drop-in exists

No project found combines multi-repository code search, message and meeting intake with cursors, a
decision journal with supersession, and MCP plus HTTP output in one self-hosted service.

## What is borrowed

| Project | What it is | Borrowed | Why not used as is |
|---|---|---|---|
| Graphiti (Zep) | Temporal knowledge graph library | Two timelines per fact (when true, when recorded); invalidate instead of delete | A library: no connectors, no scheduler, needs a graph database |
| Hindsight | Agent memory on Postgres with pgvector | One container; recall = semantic + BM25, rank fusion, cross-encoder | Push only: no source sync, no code search |
| Sourcebot | Multi-repository code search with MCP | Shape of code tools: search, read file, symbol definitions and references | Code only |
| OpenWiki | CLI that compiles a wiki with claim sidecars | Claims with versioned evidence; an update with no changes skips the model (for v2) | A CLI, no decisions, no meetings |
| Onyx, PipesHub | Enterprise search with many connectors | Connector and permission-sync model (for v3) | Several datastores; no decision journal; no claim-level evidence |
| Basic Memory, qmd | Local Markdown or SQLite knowledge base with MCP | Files as truth with a derived index; local embedding and reranking | No connectors |
| cocoindex, dlt | Incremental indexing engine; connector substrate | Candidates if own connectors grow | Not needed for v1 |

## How others handle superseded knowledge

| Approach | Who | Cost at intake | Result |
|---|---|---|---|
| Extract nothing: index plus recency; the model reconciles conflicts when answering | Onyx, Glean, Unblocked | No LLM calls | Search only; no list of decisions |
| Overwrite memory: the model picks add, update or delete | mem0, Supermemory | 1–2 calls per record | Current state; the past is overwritten |
| Bi-temporal facts: the old fact stays and gets an end date | Graphiti | Several calls per episode, plus a graph database | Current state and history |
| Compiled wiki: the article is rewritten, the old claim removed, history in git | LLM-wiki pattern, OpenWiki | A strong model per article, plus review | Coherent text; the most expensive |

Memini takes the third approach in a cheap form: decisions and open questions only, stored in plain
tables, with one extraction call per thread or meeting and one comparison call per extracted item.

## MCP as a sync transport

No mainstream product syncs in bulk through generic MCP. They index through native connectors and use MCP
for live actions or as output. The reasons:

- Tools return text shaped for a model: truncated, without `updated_since`, without a deletion feed, with
  unstable ids.
- Paging is defined per tool, not by the protocol.
- Most hosted servers need interactive consent and then a rotating refresh token that a headless client
  must store safely.

Memini therefore treats MCP as a best-effort path behind a declarative recipe per server, stores raw
responses verbatim and keeps the model out of the sync loop.

## Storage

Postgres only: pgvector for dense search, built-in `tsvector` with stemming for lexical search,
`pg_trgm` for exact code search, plain tables for persons and supersession links. Cursors, records
and jobs share one transaction.

| Alternative | Why not |
|---|---|
| Qdrant | Good hybrid search, but a second stateful store beside a relational database |
| SQLite with sqlite-vec | Vector extension was pre-1.0; fits a single-user lite mode at most |
| LanceDB | No relational side |
| ParadeDB `pg_search` | AGPL-3.0 |
| Timescale `pg_textsearch` (BM25) | PostgreSQL License, but not in the pgvector image and loaded through `shared_preload_libraries`; ranked no better than `tsvector` on the eval (requirements, section 9, Lexical search) |
| Kuzu | Archived in 2025 |
| FalkorDB, Neo4j | SSPL; GPL with a JVM. A graph database is not needed for v1 |
