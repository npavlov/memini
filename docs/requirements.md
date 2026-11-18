# Memini: requirements (draft v0)

The name is Latin for "I remember". Background on comparable projects: [prior-art.md](prior-art.md).
Build order: [plan.md](plan.md).

## 1. Purpose

A self-hosted knowledge service for an engineer or a team lead. It takes in code repositories, work
messages and meeting notes, and answers three kinds of questions:

1. **Code**: where and how is X implemented, across many repositories.
2. **Decisions**: what was decided about X, when, by whom, in which thread or meeting, and what it replaced.
3. **My context**: which meetings did I attend about X, which messages were sent to me or mention me.

Consumers are AI agents through MCP and scripts through an HTTP API. The service retrieves; the calling
agent writes the answer.

### Non-goals for v1

- LLM-compiled wiki articles with per-claim evidence markers (v2, section 11).
- Multi-user login, roles and permissions (the schema is ready for them, section 8).
- A chat UI and server-side answer generation.
- Bulk sync through native Slack or mail connectors (v1 reaches them through MCP recipes, section 4).

## 2. Principles

1. **Raw records are the source of truth.** Every index and every extracted item is derived and can be
   rebuilt from raw records at any time.
2. **Evidence first.** A knowledge item always points to the raw record it came from: a thread key, a
   meeting id, or `repo@sha:path#symbol`. An item without evidence is not stored.
3. **Trust order: code, then documents, then conversations.** Code is a fact at a commit. A message is a
   claim by a person at a time. Every search result carries its source kind and timestamp so the consumer
   can weigh it.
4. **Invalidate, never delete knowledge.** A new decision that replaces an old one links to it. The old
   one leaves the default view and stays in history.
5. **No LLM in the sync loop.** Fetching and storing raw data is deterministic. An LLM runs only in the
   derive stage, which can be re-run.
6. **Deterministic ids and upsert.** Re-ingesting the same content changes nothing. An unchanged content
   hash is never re-embedded.
7. **A cursor moves only with its data.** A stream checkpoint advances in the same transaction that stores
   a complete window.
8. **Local by default.** Embeddings and reranking run locally. The LLM provider is set per workspace; a
   cloud provider is an explicit opt-in per workspace and can be denied per source.
9. **Fail closed on index mismatch.** A changed embedding model or chunker changes the index fingerprint
   and blocks search on the old index until it is rebuilt.
10. **Freshness is visible.** Every stream reports its lag and last result; one status call says whether
    the base is current.
11. **Private stays private.** Messages and meetings never leave the store unless the user exports them.
    Secrets and card numbers are redacted at ingest. Retrieved text is data, never instructions.
12. **Simplest complete system.** One service process and one database.

## 3. Data model

Three layers: raw, index, knowledge.

### Scoping

| Entity | Meaning |
|---|---|
| Workspace | Isolation unit: a company or a project. Every row carries `workspace_id`. |
| Principal | The user. One in v1. Every row carries `owner_id`. |
| Token | Bearer token (stored hashed) bound to a principal, with scopes `read`, `ingest`, `admin`. |

### Raw layer

| Entity | Meaning |
|---|---|
| Source | A configured origin. Kind: `git`, `push`, `mcp`. Holds config, `visibility` (`private` or `workspace`), `retention_days`, `llm_policy` (`none`, `local`, `cloud`). |
| Stream | The unit of incremental sync inside a source: a git ref, a channel, a mailbox query, a meeting list. Holds the cursor, `checked_until`, schedule, last result and lag. |
| Record | One raw unit. Kinds: `code_file`, `thread`, `meeting`, `document`. Fields: deterministic `id`, `external_id`, `external_url`, `content_hash`, `occurred_at`, `ingested_at`, `deleted_at`, raw payload stored verbatim, normalized text. |
| Person | A human, with aliases: email, chat user id, meeting display name, git author. One person is marked as "me". |

Notes:
- Code is not copied into the database. The service keeps its own bare mirror of each repository; the
  mirror is the raw store, the database holds the file list at the pinned commit. The service never touches
  a user's working copy.
- A `thread` holds messages with author, time, text, recipients and mentions. A `meeting` holds title,
  start and end, participants, summary, action items and transcript.
- Person resolution uses deterministic keys first (email, chat user id). Unmatched names stay as separate
  persons until the user merges them.

### Index layer (derived)

| Entity | Meaning |
|---|---|
| Chunk | A piece of a record with text, dense vector, lexical index entry and metadata. Id = hash of record id and symbol or section. |
| Symbol | A code definition: name, kind, repository, path, span, parent. |

### Knowledge layer

A knowledge item is a `decision` or an `open_question`:

| Field | Meaning |
|---|---|
| `statement` | One or two sentences: what was decided or asked. |
| `decided_at` | When it was said (event time). |
| `participants` | Persons involved. |
| `evidence` | One or more record references, with a message anchor inside a thread or meeting. |
| `status` | `active`, `superseded`, `closed`. |
| `supersedes` | Ids of earlier items this one replaces or answers. Stored only on the new item. |
| `recorded_at`, `invalidated_at` | When the service learned it and when it stopped being current. |
| `extractor` | Model and prompt version, or `manual`. |

The journal is append-only. Corrections are new items or supersession links, never edits of old items.

## 4. Intake

### One ingest contract

Everything enters through one operation: a batch of records for a source, each with `external_id`, kind,
`occurred_at` and payload. A batch may claim a stream window (`from`, `to`, `complete`). The checkpoint
advances only when the window is complete, in the same transaction. Ingest is idempotent. Deletions arrive
as tombstones or through a full-snapshot reconcile.

### Three paths

| Path | What | v1 |
|---|---|---|
| Native connector | The service syncs by itself with a cursor. | `git`: mirror clone, scheduled fetch, pinned commit per tracked ref, re-chunk only changed files. Local paths and remote URLs. |
| Push API | Any client sends records or documents. | Yes. This is the base contract; the other paths end in it. |
| MCP recipe | A declarative description of how to sync one MCP server. | Yes, with two executors (below). |

### MCP recipes

MCP tools are built for agent queries, not for bulk sync: they return text shaped for a model, lack
`updated_since` and a deletion feed, and page per tool. A recipe pins down one server:

- the list tool and its window arguments, the detail tool, the paging rule;
- where the id, the timestamp and the reply count live in the response;
- completeness checks (page numbering, reply counts, every cursor followed);
- a deterministic parser from raw responses to records.

One recipe, two executors:

1. **Service executor**: the built-in MCP client runs the recipe on a schedule. Needs a token the service
   can hold (API key, or OAuth with a refresh token stored encrypted).
2. **Agent executor**: for servers reachable only inside an interactive agent session. The service issues
   a plan (which calls, which windows); the agent makes the calls; a hook posts each raw response verbatim
   to the service; the same parser and the same checks run server-side. The agent never rewrites payloads.

Raw responses are stored verbatim in both cases, so a parser fix can be replayed without calling the
source again.

### Scheduling and hygiene

- Per-stream schedule, with an overlap margin behind the checkpoint to catch late replies.
- First sync loads a short recent window; older history loads in steps on later runs.
- Retention per source: expired raw records and their chunks are removed on every run. Knowledge items
  outlive their raw evidence and keep the link.
- Filters per source: allowlists (channels, domains), exclusion patterns (bots, automated mail, meeting
  titles).
- Separate locks per source, so a slow code index does not block message intake.
- Every run writes a report: steps, durations, record counts, LLM tokens.

## 5. Derive pipeline

| Step | How |
|---|---|
| Chunking | Code: tree-sitter, by class and function, small siblings merged, line windows as fallback. Threads: message groups. Meetings: summary sections and transcript windows. Documents: headings. |
| Dense vectors | Local multilingual embedding model, pinned in config with its digest. |
| Lexical | BM25 in Postgres with a per-language configuration. Trigram index for exact and regex code search. |
| Rerank | Local cross-encoder, optional. |
| Decision extraction | One LLM call per new or changed thread or meeting, structured output. Skipped when `llm_policy` is `none`. |
| Supersession | For each extracted item: fetch the top-k similar active items of the workspace, one LLM call classifies it as new, duplicate, or replacing or answering specific items. |

Extraction is idempotent per record content hash and extractor version. A new extractor version can re-run
over stored records. The user can add, supersede or retract items by hand; manual items are never
overwritten by extraction.

## 6. Query surface

### MCP tools

| Tool | Purpose |
|---|---|
| `search` | Hybrid search over everything. Filters: workspace, kinds, repository, source, person, time range. |
| `search_code` | Code search. Modes: semantic, exact, regex, symbol. Filters: repository, path glob, language. |
| `read_source` | A file or a line range at the pinned commit or a given ref. |
| `get_symbol` | Definitions of a symbol, with location. |
| `list_decisions` | Decisions and open questions by query, person and time. Active by default; history on request. |
| `get_decision` | One item with evidence and its supersession chain. |
| `list_meetings` | Meetings by participant (default: me), time and query. |
| `get_meeting` | Summary, action items, participants, transcript on request. |
| `list_messages` | Messages to me, mentioning me, or from a person, by source and time. |
| `get_thread` | A whole thread. |
| `status` | Stream lag, last results, index state. |
| `add_document`, `record_decision`, `supersede_decision` | Writes; off unless the token has the `ingest` scope. |

Every result carries source kind, locator or URL, timestamp, and `repo@sha` for code. Decisions carry
status and evidence links.

### HTTP API

Mirrors the MCP tools and adds administration: workspaces, sources, streams, runs, ingest, recipe plans,
persons and aliases, reindex, forget (hard delete of a record and everything derived from it).

## 7. Service and deployment

- One process: HTTP API, MCP over streamable HTTP, scheduler and workers. One Postgres database.
- `docker compose up` is the supported install. Binds to localhost by default.
- A CLI wraps the API for setup and operations.
- Secrets come from the environment or a secrets file, never from the database in clear text.
- Health and status endpoints, structured logs, run reports.
- Backup is a database dump. Repository mirrors are re-cloned.

## 8. Growing to several users

v1 has one user. The design keeps the door open without building the feature:

- `workspace_id` and `owner_id` on every row; `visibility` on every source.
- Tokens resolve to a principal.
- Every read goes through one authorization filter. In v1 it lets everything through.

Adding users later means adding login, filling the filter and capturing source permissions at ingest. It
does not mean migrating data.

## 9. Stack

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.12+, `uv` | tree-sitter, embedding and MCP libraries; experience from the predecessor |
| API | FastAPI | — |
| MCP | Official Python SDK, server and client | — |
| Database | Postgres with pgvector | One stateful store; cursors, records and jobs in one transaction |
| Lexical search | Postgres BM25 extension or built-in `tsvector`; `pg_trgm` | Open question 1 |
| Embeddings | Local, in-process or through an OpenAI-compatible endpoint (Ollama) | Open question 2 |
| LLM | OpenAI-compatible endpoint for local models; cloud providers as opt-in | Principle 8 |

## 10. Quality

- **Retrieval eval**: a question set per workspace with expected records; recall@k per kind. Run on every
  change of model, chunker or reranker.
- **Decision eval**: labelled synthetic threads; precision and recall of extraction, accuracy of
  supersession.
- **Tests**: idempotent ingest, cursor safety on failure, reindex equals the first index, recipe parsers on
  recorded responses.
- Fixtures in the repository are synthetic only.

## 11. Later

| Version | What |
|---|---|
| v1.x | Topics per workspace and tagging by similarity; webhooks; native Slack and mail connectors; SCIP symbol graphs; a small web UI. |
| v2 | Compiled wiki: articles with per-claim evidence, incremental recompile routed by changed files, Markdown export to git. |
| v3 | Several users: login, source permissions captured at ingest, filtering at query time. |

## 12. Open questions

1. BM25: `pg_textsearch` (Postgres 17+) or built-in `tsvector`. Needs a check of Russian stemming and
   ranking quality on the eval set.
2. Default embedding model and how it ships: in-process ONNX (no extra service) or Ollama.
3. Postgres in the same image (one container) or as a second compose service.
4. Source configuration: database managed through CLI and API, or a declarative file applied on start.
5. Smallest local model that extracts decisions well enough. Decided by the decision eval.
6. Settled on 2026-11-18: the license is Apache-2.0.
7. Default retention for messages and meetings.
8. Name collision. On 2026-11-18 `memini` was free on PyPI, while npm `memini` and the GitHub repository
   `eleboucher/memini` were small active projects in the same niche (memory for AI agents). Reserve the
   PyPI name before the first public push, or rename.
