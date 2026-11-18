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
| Workspace | Isolation unit: a company or a project. Every row carries `workspace_id`. Holds `llm_policy` (`none`, `local`, `cloud`); `cloud` is its opt-in to a cloud LLM (section 9). |
| Principal | The user. One in v1. Every row carries `owner_id`. |
| Token | Bearer token (stored hashed) bound to a principal, with scopes `read`, `ingest`, `admin`; optionally limited to one workspace. |

### Raw layer

| Entity | Meaning |
|---|---|
| Source | A configured origin. Kind: `git`, `push`, `mcp`. Holds config, `visibility` (`private` or `workspace`), `retention_days`, `llm_policy` (`none`, `local`, `cloud`). |
| Stream | The unit of incremental sync inside a source: a git ref, a channel, a mailbox query, a meeting list. Holds the cursor, `checked_until`, schedule, last result and lag. |
| Record | One raw unit. Kinds: `code_file`, `thread`, `meeting`, `document`. Fields: deterministic `id`, `external_id`, `external_url`, `content_hash`, `occurred_at`, `ingested_at`, `deleted_at`, raw payload stored verbatim, normalized text. |
| Person | A human, with aliases: email, chat user id, meeting display name, git author. One person is marked as "me". |

Notes:
- A record id is a hash of its source, kind and external id, so the same record always gets the same id;
  the stream it arrived through is not part of it.
- Code is not copied into the database, nor are the Markdown documents of a repository. The service keeps
  its own bare mirror of each repository; the mirror is the raw store, the database holds the file list at
  the pinned commit. The service never touches a user's working copy.
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
| `evidence` | One or more record references, with a message anchor inside a thread or meeting, a commit in code. |
| `status` | `active`, `superseded`, `closed`, `retracted`. |
| `supersedes` | Ids of earlier items this one replaces or answers. Stored only on the new item. |
| `recorded_at`, `invalidated_at` | When the service learned it and when it stopped being current. |
| `extractor` | Model and prompt version, or `manual`. |

The journal is append-only. Corrections are new items or supersession links, never edits of old items.

**Knowledge journal** (decided 2026-11-18). The database enforces the journal, not only the code
that writes it. Triggers refuse any change of a stored item (statement, kind, `decided_at`,
extractor, times), of its evidence, participants and links, and every delete or truncate except of
a whole workspace. The status changes once, from `active`: to `superseded` by a link from a newer
item that `replaces` it; to `closed` by a link from a decision that `answers` an open question, or
by hand without a replacement; to `retracted` by hand when the item was wrong (a misread extraction,
a mistake). `invalidated_at` is set with the change to the time of its transaction, as `recorded_at`
is set to the time of the transaction that records the item; the database sets both. Retracted is a
status, not a separate mark: an item is current or not for one reason, and the history shows which.
A link is a row stored on the newer item only; inserting it supersedes the earlier item in the same
statement. Both items must be active and in one workspace, the newer one said no earlier than the
other, and `answers` goes from a decision to an open question. So an item is superseded at most
once, and links cannot close a cycle (the database checks for one anyway). Extraction never
supersedes or answers an item recorded by hand, so a manual correction survives a re-extraction. An
item without evidence is not stored, a manual one included: the database checks at commit that it
has evidence, and evidence and participants are written in the transaction that records their item.
A decision taken in a conversation is first stored as a document, then cited. Evidence is a record
id and an anchor: the message id in a thread; a segment, offset or section in a meeting; the commit
in a code file (`<sha>`, `<sha>#symbol`, `<sha>#L12-L30`); a heading trail, lines or nothing in a
document. It has no foreign key to the record and keeps a copy of the record's source, kind,
external id and URL, which the database takes from the record. Retention, forget and deleting a
source therefore leave the item and its reference, shown as gone, and a record ingested again is
found by its unchanged id. Evidence holds no quote, so no text of a record outlives the record. A
person cited as a participant cannot be deleted. Extraction is idempotent through one row per
record, content hash and extractor (model and prompt version), written with the items found, also
when none were: the same content read again by the same extractor writes nothing. Tables:
`knowledge_item`, `knowledge_evidence`, `knowledge_participant`, `knowledge_supersession`,
`knowledge_extraction`; library: `memini.knowledge`.

## 4. Intake

### One ingest contract

Everything enters through one operation: a batch of records for a source, each with `external_id`, kind,
`occurred_at` and payload. A batch may claim a stream window (`from`, `to`, `complete`). The checkpoint
advances only when the window is complete, in the same transaction. Ingest is idempotent. Deletions arrive
as tombstones or through a full-snapshot reconcile.

**Ingest** (decided 2026-11-18). One library, `memini.ingest`, is the contract: the push API
(`POST /v1/workspaces/{workspace}/sources/{source}/records`, scope `ingest`, inside the token's
workspace limit) and the MCP recipe executors (M3) end in it. It takes batches for push and mcp
sources; a git source is refused (409 `not_ingestible`), its records come from its mirror. A push
source is a name and the shared settings (visibility, retention, LLM policy): no location, refs or
schedule, never synced. An item is a record of kind `thread`, `meeting` or `document` (`code_file`
is not ingestible) with `external_id`, `occurred_at` with a zone, an optional http(s)
`external_url` and a payload, or a tombstone (`deleted`, with the kind and external id only); a
kind and external id appear once per batch. The payload is a JSON object with the fields its kind
requires: a thread's `messages`, each with `author`, `sent_at` and `text`; a meeting's `title` and
`started_at`, and `ended_at` not before it; a document's `title` and `text`. Other fields are kept
for the parsers of the kinds. It is stored as canonical JSON (keys sorted, no whitespace, UTF-8,
numbers as Python writes them), not as the bytes of the request: the same object from another
client, in another key order or spacing, is the same content and changes nothing (principle 6),
and redaction at ingest can rewrite it. Nothing a parser reads is lost: an object with a key twice
is refused rather than collapsed, and a value that is not finite JSON is refused. The content hash
is SHA-256 of the stored bytes. A record whose payload, time, URL and stream equal its row is not
written at all; one that differs is written, a tombstone revived, and a changed payload clears the
derived text. A batch may name a stream, created on first use, and claim a window `[from, to)` of
it; batches of one stream take turns on the stream's row. The checkpoint (`checked_until`, with
the claim's optional `cursor`) moves to `to` only for a complete window that ends after it, by
compare-and-set in the transaction that stores the records, so it never moves back. A complete
window must start at or before the checkpoint (an overlap margin); one that starts after it is
refused (409 `window_gap`), as it would count the gap as checked. An incomplete window stores its
records and leaves the checkpoint, and its run is `partial`; a window older than the checkpoint
(history loaded later) stores its records and leaves it too. A window may end at most five minutes
past the database clock. A snapshot, a complete window with `snapshot`, holds every live record of
the stream whose `occurred_at` is in the window: the stream's records there that it leaves out
become tombstones. A tombstone sets `deleted_at` and deletes the record's chunks in the same
transaction; one for a record never stored changes nothing. Any failure rolls back records,
tombstones, chunks and checkpoint together (principle 7). Each batch writes a run of kind `ingest`
with its counts. Limits answer 413: the body (`ingest_max_body_size`, 16 MiB, refused by its
Content-Length before it is read), the items of a batch (`ingest_max_records`, 1000) and a stored
payload (`ingest_max_payload_size`, 1 MiB). A batch that is not valid answers 422 naming the
field, never a value. No idempotency key: the content hash and the compare-and-set already make a
resend harmless. Chunking is a hook in the batch's transaction that the parsers of the kinds fill;
until they do, pushed records are stored without chunks.

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
| Dense vectors | Local multilingual embedding model, pinned in config with its digest: Qwen3-Embedding 0.6B through Ollama by default. |
| Lexical | BM25 in Postgres with a per-language configuration. Trigram index for exact and regex code search. |
| Rerank | Local cross-encoder, optional. |
| Decision extraction | One LLM call per new or changed thread or meeting, structured output. Skipped when `llm_policy` is `none`. |
| Supersession | For each extracted item: fetch the top-k similar active items of the workspace, one LLM call classifies it as new, duplicate, or replacing or answering specific items. |

Extraction is idempotent per record content hash and extractor version. A new extractor version can re-run
over stored records. The user can add, supersede or retract items by hand; manual items are never
overwritten by extraction.

**Code chunks** (decided 2026-11-18). One chunk per definition: function, method, class, type,
constant. A comment block right above a definition, and its decorators, belong to it; statements
outside definitions are chunks of their own. A class or module of more than 40 lines is split into
its members; neighbours of at most 6 lines merge while the merged chunk spans at most 40 lines;
anything over 80 lines or 3000 characters is cut into windows of 60 lines that overlap by 10. A file
without a grammar, or larger than 1 MiB, is cut into windows only; a binary file (a NUL byte in
its first 8000 bytes) gives no chunks. Bytes that are not UTF-8, and NUL bytes further on, become
U+FFFD in the chunk text: Postgres text cannot hold a NUL, and the content hash is computed over
the text that is stored. The text embedded is a header line (path and qualified names) and the
source lines. A chunk id is a hash of the record id and a structural key: the qualified name of the
definition with an ordinal for repeated names, or for statements a count from the definition
before them. Line numbers and content are not in the key, so editing one function changes the
content hash of its chunk and nothing else. The chunker version and the grammar versions are part
of the index fingerprint.

**Document chunks** (decided 2026-11-18). A Markdown document is cut by its headings: ATX (`#` to
`######`) and setext (a paragraph underlined with `=` or `-`), as CommonMark has them at the top
level; no line of front matter, of a fenced code block or of an HTML comment is a heading. A
section is a heading and its text up to the next heading of any level; the text before the first
heading, front matter included, is a section too. Each section is one chunk of kind `section`; one
over 80 lines or 3000 characters is cut into windows of whole blocks (runs of non-blank lines; a
fenced block or a comment is one block, blank lines inside it included) of at most 60 lines, and
a block larger than that into line windows as code is. Neighbours of at most 6 lines merge into a
`group` while it spans at most 40 lines and stays under the heading that encloses the first of
them; a section of at most 6 lines also goes with its first subsection when the two fit 80 lines,
as a class line goes with its first member, so a heading without text of its own is never a chunk
alone. The text embedded is a header line, the path and the heading trail of each section
(`README.md: Shop > Install > Docker`), and the lines. A chunk id is a hash of the record id and
the trail with an ordinal for a repeated trail: editing one section changes the content hash of
its chunk and nothing else, and renaming a heading changes the ids of the sections under it.
Documents have no symbols and no language. The Markdown chunker has its own version in the index
fingerprint. The Markdown files of repositories are documents (code sync, section 9); a pushed
document is chunked the same way from its payload until its intake (M2) decides otherwise.

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

Mirrors the MCP tools and adds administration: workspaces, sources, streams, runs, tokens, ingest,
recipe plans, persons and aliases, reindex, forget (hard delete of a record, its raw responses and
its index; knowledge items that cite it stay and keep their reference, section 3).

**Source administration** (decided 2026-11-18, open question 4). Source configuration lives in the
database and is managed through the HTTP API and the CLI; there is no configuration file applied on
start. An apply-from-file command can come later on top of the same calls; it is not built. The routes
are under `/v1`: create a workspace; list, add, read, change and delete the sources of a workspace;
list the streams of a source with their lag, last run and queued sync; trigger a sync; list the runs of
a workspace, a source or a stream, newest first and paged; read one run with its report. Reads need
the `read` scope, changes the `admin` scope. A push source is a name and the shared settings; the
batches sent to it create its streams (section 4). A git source is a location, the refs it tracks and path
globs: `source.config` holds `{"location", "include", "exclude"}`, each tracked ref is a stream keyed
by its full name (`refs/heads/main`), and the schedule is set on all its streams at once. The location
is checked when the source is added, by the rules the mirror enforces at every fetch (section 9, git
mirrors), so a bad one is refused at once with a message that never repeats it; it never holds a
credential, and it cannot change: another repository is another source. A changed schedule starts
now. A triggered sync enqueues one job per stream with the stream's dedupe key, so it merges with a
scheduled one. Deleting a source deletes its streams, runs, jobs and records and removes its mirror in
the same request, under the source's job lock; while a job of the source runs, the deletion is refused
and can be retried.

**Token administration** (decided 2026-11-18). `POST /v1/tokens` creates a token for the caller's
principal and returns it once: only its digest is stored, and the answer is not cached.
`GET /v1/tokens` lists tokens without secrets, and `DELETE /v1/tokens/{id}` revokes one; revoking
again keeps the first time. All three need the `admin` scope. That scope manages tokens, so it may
give a new token any scope; what bounds the new token is the caller's workspace limit: a token
limited to one workspace creates only tokens limited to it (one for every workspace is refused,
another workspace is not found) and sees and revokes only those. The first admin token cannot come
from the API; `memini bootstrap` makes it (section 7).

**Query tools** (decided 2026-11-18). `search`, `search_code`, `read_source` and `get_symbol` are
one library, `memini.query`, behind the MCP tools and their HTTP routes, with the same arguments and
results: `GET /v1/search`, `GET /v1/search/code`, `GET /v1/symbols` and
`GET /v1/workspaces/{workspace}/sources/{source}/files/{path}`. `search` ranks every kind of record
(modes `hybrid`, `dense`, `lexical`); the person filter waits for M2. A hit carries its workspace,
source and source kind, record kind, a locator (`repo@sha:path#L12-L30` for a file of a repository,
code or Markdown), `occurred_at`, for such a file the repository, ref and commit, and the chunk's
text. `search_code` and `get_symbol` look at code records only. `read_source` reads the service's
mirror and never clones or fetches: without a ref, at the pin of the tracked ref the mirror's `HEAD`
names (else the first tracked ref); a tracked ref at its pin, so a read agrees with the commit of a
hit; another branch or tag at the commit the mirror has; a full commit id if the mirror holds it;
revision expressions are refused. A path is relative to the repository root; absolute paths, `.` and
`..` components and control characters are refused before git runs, and a path is looked up in the
commit's tree, so it names nothing outside the repository and no symlink is followed: a symlink
reads as its target text, a submodule as the commit it pins, a directory as a listing, a binary file
as its size. A file over `git_max_blob_size` is refused; a read returns at most 2000 lines and
100,000 characters and says where to continue. A refusal is a tool error with a message for the
model and, over HTTP, the API's error shape: 422 for a query, path, ref or range that is not valid
and for a search past `search_timeout` (the query's to narrow), 404 for anything the token may not
read, 409 for a repository not synced yet, 503 for a stale index (the message names
`memini reindex`) or no embedding provider. Every search mode needs the embedder, whose spec the
fingerprint check compares (principle 9); reading code needs none. `serve` makes one embedder for
search and sync, so a local model is loaded once.

## 7. Service and deployment

- One process: HTTP API, MCP over streamable HTTP, scheduler and workers. One Postgres database.
- `docker compose up` is the supported install. Binds to localhost by default.
- A CLI wraps the API for setup and operations (decided 2026-11-18). Its commands are HTTP clients of
  the running service, with a service URL and a token: the service is the one place that checks scopes,
  applies the authorization filter, touches the mirrors and writes run reports, and no database
  credentials leave the service. A few operator commands work on the database directly, inside the
  service's container, because they run before a token exists or below the API: migrations, the first
  admin token (bootstrap), the credentials of sources and key rotation, a workspace's opt-in to a cloud
  LLM (`memini workspace llm`, which no token may give), a full reindex and the eval. A client command reads the service URL
  from `MEMINI_URL` (default `http://127.0.0.1:6364`) and the token from `MEMINI_TOKEN` or the file
  `MEMINI_TOKEN_FILE` names; no option takes the token itself, which shell history and the process
  list would keep. `memini bootstrap` gives the configured principal a token with the `read` and
  `admin` scopes for every workspace and prints it once; while that principal has an active admin
  token for every workspace it refuses, unless forced.
- Secrets come from the environment or a secrets file, never from the database in clear text.
  The credentials of sources are stored encrypted, under a key from the environment (section 9).
- Health and status endpoints, structured logs, run reports.
- Backup is a database dump. Repository mirrors are re-cloned.

## 8. Growing to several users

v1 has one user. The design keeps the door open without building the feature:

- `workspace_id` and `owner_id` on every row; `visibility` on every source.
- Tokens resolve to a principal.
- Every read goes through one authorization filter. In v1 it lets everything through except what the
  token itself limits: a token limited to one workspace reads only that workspace.
- Every write checks its scope and the same limit: a token limited to one workspace changes nothing
  outside it and creates no workspace.

Adding users later means adding login, filling the filter and capturing source permissions at ingest. It
does not mean migrating data.

## 9. Stack

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.12+, `uv` | tree-sitter, embedding and MCP libraries; experience from the predecessor |
| API | FastAPI | — |
| MCP | Official Python SDK, server and client | — |
| Database | Postgres with pgvector | One stateful store; cursors, records and jobs in one transaction |
| Database access | psycopg 3, async, plain SQL; `psycopg_pool`; the `pgvector` adapter | The SQL later work needs is Postgres SQL; typed; async like FastAPI and MCP. See below |
| Migrations | Numbered SQL files and a small runner in `memini.db` | Forward only; one transaction per file; an advisory lock for concurrent starts. See below |
| Lexical search | Postgres BM25 extension or built-in `tsvector`; `pg_trgm` | Open question 1 |
| Embeddings | Local, in-process or through an OpenAI-compatible endpoint; by default Qwen3-Embedding 0.6B in Ollama | Open question 2, settled |
| LLM | OpenAI-compatible endpoint for local models; cloud providers as opt-in | Principle 8 |

**Database access** (decided 2026-11-18). Memini targets one database. What later work needs is Postgres
SQL: a stream cursor and the records of its window in one transaction, jobs claimed with
`FOR UPDATE SKIP LOCKED`, per-source advisory locks, upserts by deterministic id with `ON CONFLICT`,
`vector` columns. psycopg 3 runs that SQL as written. SQLAlchemy Core would add a second language over the
same SQL, and its async mode runs through a greenlet bridge; its main gain, portability across databases,
is not needed. asyncpg is async only and is not a typed package. psycopg types queries as
`LiteralString`, so a query cannot be built by string formatting; identifiers go through `psycopg.sql`.
Access is async because FastAPI, the MCP server and the MCP client are async; CPU-bound work (embeddings,
parsing) goes to threads either way. The CLI runs the same code with `asyncio.run`. One `psycopg_pool`
pool serves the process; each pooled connection registers the pgvector types.

**Migrations** (decided 2026-11-18). Alembic is built on SQLAlchemy, and its main gain, autogenerate,
needs SQLAlchemy models. Migrations are files `src/memini/db/migrations/NNNN_name.sql`, numbered from
0001 without gaps. The runner takes a session advisory lock, so a second process that starts at the same
time waits and then finds nothing to do. Each file and its row in `schema_migration` commit in one
transaction. The checksum of every applied file is kept; an edited migration, or a database migrated by a
newer build, stops the runner. Migrations go forward only; a backup is the way back (section 7). A step
that cannot run in a transaction, such as `CREATE INDEX CONCURRENTLY`, is not supported yet. `pg_trgm` is
a trusted extension, so the owner of the database can create it. `vector` is not: on a server where the
service role is not a superuser, an administrator runs `CREATE EXTENSION vector` once before the first
migration. The compose database runs as a superuser and needs nothing extra.

**Jobs** (decided 2026-11-18). Work the service starts on its own (syncs, retention, reindex) is a row in
the `job` table; workers claim rows with `FOR UPDATE SKIP LOCKED`, and there is no broker. A job can be
enqueued in the same transaction as the rows that call for it. A claim carries a lease that the worker
renews while the job runs; a lease that runs out means the worker died or hung, and the job goes back to
the queue with the lost attempt counted. A failed attempt is retried after an exponential backoff; after
`max_attempts` the job is quarantined until a person looks at it. One job per source runs at a time: the
worker holds a session advisory lock on the source (the two-key form, apart from the migration lock) on
the connection that renews the lease, so the lock goes with a dead worker's connection, and a hung worker
keeps the source until it finds its lease gone and stops. On shutdown a worker claims nothing more, lets
running jobs finish within a grace period and returns the rest to the queue without counting the attempt.
The scheduler enqueues a sync of each stream whose `schedule` is due, by the database clock; a dedupe key
per stream keeps at most one sync of a stream queued or running, also with several service processes.

**Embeddings and the index fingerprint** (decided 2026-11-18). One async interface, `memini.embeddings`,
with two providers: an OpenAI-compatible `/embeddings` endpoint (Ollama, llama.cpp, vLLM or a cloud
service; retries with backoff, a timeout per attempt) and an ONNX model in the process. The in-process
provider needs the optional extra `memini[local-embeddings]` (onnxruntime, tokenizers, numpy: about 150 MB
installed), so the base install and image go without it. Its model files are pinned by a digest over
their paths and SHA-256, verified before they are loaded and fetched at a pinned commit when missing; files
that do not match are never used. It runs the model once per text: in a padded batch a quantized model
makes a text's vector depend on its neighbours. Every vector is L2-normalized and rounded to float32.
Embeddings are off until a provider is set; the endpoint, local or cloud, is the user's explicit choice
(principle 8). The fingerprint is a digest over the provider, model, digest, dimension, normalization,
pooling, token limit, document prefix and chunker version; each workspace records the one its index was
built with (`index_state`), and search or indexing under another one is refused with a message that names
`memini reindex` (principle 9). A vector is reused by its text hash and the embedding part of the
fingerprint, so an unchanged text is never embedded twice, also across chunker versions (principle 6).
The default model is settled in the next paragraph.

**Default embedding model** (decided 2026-11-18; open question 2). Qwen3-Embedding 0.6B
(`qwen3-embedding:0.6b`, 1024 dimensions, Q8_0), served by Ollama through the OpenAI-compatible
provider. The local provider cannot run it: it pools by the mean or the first token, Qwen3-Embedding
by its last one. Embeddings stay off until a provider is set, so the service starts without Ollama.
`embedding_provider = openai_compatible` and nothing else means the default: the URL
`http://127.0.0.1:11434/v1` (Ollama on this machine), the model, and for that model only its
dimension, the digest of the Ollama build the eval measured
(`ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`) and its query instruction. A
value that is set wins; another model gets none of the three and needs its own dimension. The
defaults live in `memini.config`, so the CLI on the host, the eval and the container agree. The
digest goes into the fingerprint but is not checked: the OpenAI-compatible API does not report it,
so after `ollama pull` brings a new build the user sets its digest and reindexes. Qwen3-Embedding
embeds a query after an instruction, `Instruct: {task}\nQuery:`, and a document as it is; the
document prefix stays empty. The task, measured on the code suite against seven other wordings
and none (open question 2), is "Given a search query, retrieve the relevant code or documentation
that can help answer the query". The query prefix is not in the fingerprint, so changing it needs
no reindex. The compose stack passes the `MEMINI_EMBEDDING_*` variables from the environment or
`.env` (`.env.example` lists them, commented out) and, when a provider is set without a URL,
points the URL at Ollama on the host, `http://host.docker.internal:11434/v1`; an `extra_hosts`
entry with `host-gateway` makes that name work on Linux too, where Ollama must also listen on the
Docker bridge. Compose keeps embeddings off by default as well, rather than on with Ollama
assumed: on a machine without Ollama a default-on stack would accept sources and then fail every
sync and search on a connection error, while off it names the setting to set; and the smoke test
in CI needs no model.

**Git mirrors** (decided 2026-11-18). The service runs the `git` CLI as a subprocess, not pygit2 or
dulwich: the CLI is the reference for every transport and for fetch negotiation, it has documented
switches to harden it, and a process that hangs can be killed with everything it started. pygit2 runs
libgit2 inside the service process, where a hang cannot be killed and a crash takes the service down;
dulwich is pure Python and slow on large packs. Each source has a bare mirror in its own directory,
named by the source id, under `git_mirror_dir` (a volume in the image). Only branches and tags are
mirrored. A fetch writes to a namespace readers never look at, then the size of the mirror is checked,
then branches and tags move in one ref transaction: a fetch that fails, times out or grows the mirror
past `git_max_mirror_size` changes nothing a reader sees. Every git command runs without user or system
config, hooks, prompts or submodule recursion, with only the transport of its remote allowed. Sources
are `https` and `ssh` URLs; plain `http` and local paths are refused unless a setting allows them
(`git_allow_http`, `git_local_roots`): plain http exposes code and credentials on the network, and an
unrestricted local path would let whoever configures sources read any repository the service user can
read, the mirrors of other workspaces included (principle 11). A local repository is fetched through
`upload-pack` like a remote one, never cloned with `--local`, so nothing is written to it.
Credentials are global for now: one https token, sent only to the hosts listed with it, through a
credential helper that reads it from the environment of the git process (never in a URL, in process
arguments or on disk), and one ssh key with a known_hosts file and strict host key checking.
Per-source credentials come with the credential store (M3).

**Index tables, writer and rebuild** (decided 2026-11-18). Chunks and symbols are the tables `chunk`
and `symbol`. Each row names its record with the record's workspace, source and kind in one foreign
key, so a row cannot point into another workspace, and deleting a record (forget, retention, a
deleted source) deletes its chunks and symbols. The writer runs in the caller's transaction: it
locks the record, checks the fingerprint, compares each chunk with the stored row of the same id
and writes only what differs. A vector is computed only for a text that has none in the workspace
under the embedding key; a chunk that only moved keeps its vector. Writing unchanged input again
embeds nothing and changes no row. Its counts reach the run report only once the transaction
that wrote them has committed, so a batch that rolls back reports nothing. The vector column has
no dimension, so a change of model needs no migration. The HNSW index is one partial index per embedding key on a cast of the column
(`vector` up to 2000 dimensions, `halfvec` up to 4000, none above), created by the service before
the first chunk of the key is written, and dropped by the rebuild that leaves it unused; a search
orders by the same expression, with the key as a literal. The lexical column is a `tsvector` with
the `simple` configuration (no stemming, the same for every language): a baseline for open
question 1 to measure against, not its answer. A trigram index on the content serves exact and
regex code search. `memini reindex --workspace NAME` rebuilds one workspace from raw records,
record by record through the same writer under the new fingerprint; one last transaction deletes
the chunks of other embedding keys and records the new fingerprint, so search stays refused until
it commits. A rebuild that stopped resumes without embedding anything twice. The files of git
sources, code and Markdown documents, are read again from the repository mirror (their content
hash is the blob id), in batches: two git processes read the blobs of up to a page of records of
one source, within a memory budget (8 MiB by default), not one process per file. Other records are
read from the stored payload.

**Code sync** (decided 2026-11-18). Each tracked ref of a git source is a stream; its pin, kept in
`stream.cursor`, is the commit its records are complete at and a digest of the rules that selected
them. A sync (the `git_sync` job, one stream at a time) fetches the mirror, then diffs the pin
against the commit the ref names now and writes only the files that differ; with no pin, a pin no
longer in the mirror (a force push, then gc), changed rules, or after a sync that failed part way,
it lists the ref in full and compares every file with its stored record instead. A file whose record
already holds its blob id is not read, chunked or embedded. A file's record is one file on one ref:
a Markdown file (`.md`, `.markdown`, `.mdx`, in any case) is a `document`, chunked by headings, any
other file a `code_file`; neither keeps a payload. Its external id is `refs/heads/main:src/app.py`,
its content hash the blob id, its time the committer time of the last commit that changed it on the
ref's first-parent line (a merged change has the time of the merge). One `git log` walk per sync
finds them: a diff walks the commits after the pin, a listing the newest 1000; a file not reached
gets the time of the oldest commit walked when the walk was cut short (it changed then or earlier),
else that of the synced commit. Asking `git log -1` per file was measured at about 11 ms a file,
over a minute for a first sync of 5000 files; the walk was one process of 10 to 60 ms on a synthetic
history of 100,000 commits. The kind is part of the record id and of the rules digest: when the
kinds change, the next sync lists the ref, writes each file as a record of its new kind and turns
its record of the old kind into a tombstone. A file is indexed when it is a regular file (not a
symlink or submodule) with a UTF-8 path, the source's globs select it, no built-in exclusion matches
it (lock files, minified bundles, source maps, `node_modules`; generated, large and of no use to
search; a source can switch them off with `"builtin_excludes": false` in its configuration, and the
switch is part of the rules digest), it is at most `git_max_blob_size` and it is not binary. Every
other file, and a deleted one, turns a live record into a tombstone whose chunks are deleted in the
same transaction; a file that comes back revives its record. Files are written in batches, each in
one transaction with its records and chunks, the last together with the tombstones and the move of
the pin and `checked_until` (the database time before the fetch), so a small change is all or
nothing (principle 7). A larger one commits batch by batch and marks the pin partial first: a
failure leaves the old pin, every record agrees with its chunks, and the next sync lists the ref in
full and embeds nothing twice. One big transaction was not chosen: a first sync embeds for minutes
to hours, and a failure at its end would throw all of that away. A stale index blocks the sync:
after the fetch, a workspace whose fingerprint differs refuses it with the message that names
`memini reindex`, no record changes and the pin stays; the rebuild reads code from the fetched
mirror, and the next sync catches up from the pin. Writing records without their chunks meanwhile
was not chosen: a sync writes only what changed, so records stored during a rebuild, or before
embeddings were set, would stay out of the index. For the same reason `serve` syncs git sources only
when an embedding provider is configured. A ref no longer tracked loses its stream; the next sync of
the source turns its records into tombstones. A tracked ref the remote no longer has (deleted, or
never there) is not an error to retry: after a fetch that succeeded, one transaction turns the ref's
records into tombstones, deletes their chunks and replaces the pin with a `missing` marker, moving
`checked_until`; the run counts `refs_missing`, and status shows the stream as missing (not as
behind: the base holds what the source holds). When the ref comes back, the stream has no pin, so
the next sync lists it in full and brings its records back.

**Search** (decided 2026-11-18). One library, `memini.search`, serves the MCP tools and the HTTP
API. Its modes are `hybrid` (the default), `dense`, `lexical`, `exact`, `regex` and `symbol`; the
"semantic" mode of `search_code` is `hybrid` over code records. A hybrid search takes up to
`search_candidates` (50) chunks from the dense and from the lexical retriever and merges them by
reciprocal rank fusion: a chunk scores the sum of 1 / (k + rank) over the retrievers that found
it, k being `search_rrf_k` (60). Only ranks count, so a cosine distance and a lexical rank need no
common scale. The dense retriever scans the HNSW index iteratively, so a selective filter still
fills the list. The lexical one looks for any word of the query in the `simple` tsvector, ranked
by `ts_rank_cd`: the baseline of open question 1, replaceable in one place. Exact search
(`LIKE`) and regex search (`~`, newline-sensitive, as grep reads a pattern) use the trigram
index. A search runs in one read-only transaction, so the fingerprint check and every statement
see one snapshot, and each statement under `statement_timeout` (`search_timeout`, 5 s): a regular
expression that backtracks without end is cancelled and reported as a timeout. Filters
(workspaces, sources, repositories, record kinds, languages, path globs with the rules of
`.gitignore`, a time range on `occurred_at`) apply inside the authorization filter, to every
retriever and to the final read; a tombstoned record never appears. Every mode refuses a
workspace whose index fingerprint differs, not only the modes that embed the query (principle 9).
The fingerprint covers the chunker, whose chunks, lines and symbols exact and symbol search
return. During a rebuild the index mixes old and new chunks. One stale workspace in reach refuses
the whole search, so a result is never silently incomplete. The person filter is refused until
records are linked to persons (M2), because an empty answer would read as "nobody said it". A hit
carries the kinds of its source and record, the source's name, the external id and URL,
`occurred_at`, the path, symbol and lines, and the chunk's content as stored, cut to 2000
characters (data, never instructions). For code it also carries the commit pinned in the cursor of
the record's stream.

**Credential store** (decided 2026-11-18). The service executor needs a credential for each source
it reaches on its own: an API key, or OAuth tokens with a refresh token. They are kept in the table
`credential`, one per source, encrypted with AES-256-GCM from `cryptography` under `credential_key`
(`MEMINI_CREDENTIAL_KEY` or `_FILE`: 32 random bytes in base64), which never enters the database.
The service starts without the key; the store then refuses with a message that names the setting.
The secret fields (the API key; the access token, the refresh token, the client secret) are one
encrypted JSON object; the kind, the expiry, the token endpoint, the client id and the scope stay
in clear. GCM rather than Fernet, because GCM authenticates associated data: each ciphertext is
bound to its row's id, workspace, source and kind, so a ciphertext copied into another row, or a
row whose kind was changed, fails to decrypt. Each row names its key by a key id (16 hex digits of
an HMAC under the key), so `memini credentials rotate-key` finds every row not under the current
key and re-encrypts it, in batches that commit one by one, reading it with a key of
`credential_old_keys`. The rotation is idempotent and resumable, refuses before it changes anything
when a row is under a key it lacks, and leaves the secret and its generation as they were; during
it the service runs with the new key and the old ones. A refresh runs in one transaction: it locks
the row, calls the token endpoint, writes the new tokens as the next generation and commits. The
old tokens therefore stay until the new ones are committed, and a refresh that waited for the lock
finds the newer generation and does not spend the old refresh token a second time (a provider that
detects reuse would revoke the grant). One window stays open: a provider that spends a refresh
token on use, and a crash between its answer and the commit. The new tokens then existed only in
memory and the source must be authorized again; a failure that leaves the process running hands
them back to be stored on another connection. Secret values are `SecretStr` in memory, registered
for log redaction as soon as they are decrypted, and no log line, error text or HTTP response holds
one; no route returns a secret. `memini credentials set` stores a credential read from stdin.

**LLM providers and the policy gate** (decided 2026-11-18). One async interface, `memini.llm`, for
chat completions with structured output: a request names a pydantic model, and the reply is parsed
and validated against it whatever the endpoint promised. How an endpoint is asked for JSON is set
per provider: `response_format` with the JSON schema, with `json_object`, or not at all, the schema
then in the system message; a reply in a code fence or after a `<think>` block is still read, and
one that does not validate is an error that names fields, never values. Temperature (0 by default)
and a token limit per request; retries, backoff, `Retry-After` and a timeout per attempt as for
embeddings. The tokens of every call go to the run report under `local/model` or `cloud/model`.
The settings declare at most two providers, each an OpenAI-compatible chat API: `llm_local_*`,
trusted with the content of every source, and `llm_cloud_*` (https only), trusted with sources
whose policy is `cloud` in workspaces that opted in. The slot an endpoint is set in is the
declaration; its class is never inferred from its host. Keys come from the environment only, are
registered for redaction and are removed from error text a server sends back. The workspace's
choice is a column, `workspace.llm_policy` (`none`, `local`, `cloud`; default `local`, as for
sources), and `cloud` is the opt-in. Only the CLI sets it (`memini workspace llm`), working on the
database as the operator; the HTTP API has no route for it, so no token, an admin one included,
can send content to a cloud provider the operator did not allow. Providers by name, so that two
workspaces could use two cloud accounts, are not built: the flat settings have no form for a list
of providers with keys, and one cloud provider covers v1; a later migration can add a provider name
per workspace. Every LLM call goes through one gate, `LlmGate.complete`, which takes the ids of
every source whose content is in the request, reads the policies of the workspace and of those
sources at each call and takes the strictest (`none` < `local` < `cloud`): `none` refuses; `local`
goes to the local provider and never to the cloud one, whatever the workspace allows; `cloud` goes
to the cloud provider, or to the local one when no cloud provider is configured. Content mixed from
several sources, as in a supersession check, is held to the strictest of them. The gate fails
closed: no source named, a source of another workspace or of none, an unknown policy value, no
provider the policy allows: each is a refusal before anything is sent, and a caller turns the
expected ones (`policy_none`, `no_provider`) into a skip. The class that sends is private to the
package, created only with a key the gate holds, and checks the gate's grant again before it sends:
a cloud endpoint takes only a grant whose policy is `cloud`. A test parses every module of the
service and fails on any other import of it. `llm_policy` does not govern embeddings: chunk text of
every source goes to the embedding endpoint the user set (above).

## 10. Quality

- **Retrieval eval**: a question set per workspace with expected records; recall@k per kind. Run on every
  change of model, chunker or reranker.
- **Decision eval**: labelled synthetic threads; precision and recall of extraction, accuracy of
  supersession.
- **Tests**: idempotent ingest, cursor safety on failure, reindex equals the first index, recipe parsers on
  recorded responses.
- Fixtures in the repository are synthetic only.

**Retrieval eval** (decided 2026-11-18). `memini eval` runs a suite: a TOML file of fixture
repositories and questions (`eval/code/suite.toml`, the code suite: three synthetic repositories in
two workspaces, about a hundred questions in English and Russian, a quarter of them asked in the
other language than the text that answers them). A question has an id, a text, a language, a kind
(`code`, `document` or `symbol`: what its answers are), the search modes it targets, optional
filters and tags, and its expected results. An expected result names a place in a fixture file,
never a chunk id, so a question survives a change of the chunker: a repository and a path, and a
definition (a hit matches when its lines overlap the definition's in the symbol table), a heading
(a hit matches when its header holds a trail that ends with it) or a line range. The eval commits
the fixtures to new git repositories, creates a scratch database on the configured server, indexes
them through the service's own git sync with the configured embedding model (or the FakeEmbedder,
whose vectors carry no meaning), resolves every expected result against the index (one the index
does not hold stops the run), searches each question in each mode, and drops the database. The
user's database is not touched; the role needs CREATEDB. It reports recall at k (1, 5, 10) and the
mean reciprocal rank per mode, kind, language and tag. An expected result counts once however many
hits match it, and hits with equal scores count as a tie: a metric is its expected value over the
orders of the tie, so the chunk ids that break ties in search do not move it. The report (JSON)
records the suite's digest, the embedding spec, the index fingerprint, the chunker, the lexical
retriever and the reranker; a baseline is such a report, and a run exits with code 4 when any
recall or MRR of a group falls below the baseline by more than a margin (0.02). The lexical
retriever and the reranker are chosen by name from registries of the eval (`--lexical`,
`--reranker`), and `memini.search.search` takes the lexical statement as a parameter: open question
1 adds its candidates there and measures them against `tsvector-simple`. The committed baseline,
`eval/code/baseline-fake.json`, covers the modes the FakeEmbedder does not take part in (lexical,
exact, regex, symbol) and is what CI compares with. `eval/code/baseline-qwen3.json` covers every
mode, dense and hybrid included, with the default model (open question 2). It needs Ollama on the
machine that runs the eval, with `qwen3-embedding:0.6b` pulled, so CI does not run it. Rerun it
after a change of the model, the chunker, the lexical retriever or the fusion, and write it again
when a change is meant to move it (about 10 s):

    make eval-qwen3                     # every mode, compared with the baseline
    MEMINI_EMBEDDING_PROVIDER=openai_compatible memini eval --modes all \
        --baseline eval/code/baseline-qwen3.json --write-baseline   # write it again

Dense results are the same on every run. Hybrid results move by up to about 0.03 in a group from
run to run (one of four reruns flagged a drop of 0.021): lexical ties are broken by chunk id, and
chunk ids derive from the source ids, which are new in every scratch database. Until that is fixed,
a hybrid regression of that size is noise; compare with `--margin 0.05` to be sure.

## 11. Later

| Version | What |
|---|---|
| v1.x | Topics per workspace and tagging by similarity; webhooks; native Slack and mail connectors; SCIP symbol graphs; a small web UI. |
| v2 | Compiled wiki: articles with per-claim evidence, incremental recompile routed by changed files, Markdown export to git. |
| v3 | Several users: login, source permissions captured at ingest, filtering at query time. |

## 12. Open questions

1. BM25: `pg_textsearch` (Postgres 17+) or built-in `tsvector`. Needs a check of Russian stemming and
   ranking quality on the eval set.
2. Settled on 2026-11-18 by the owner: Qwen3-Embedding 0.6B (`qwen3-embedding:0.6b`, 1024 dimensions,
   multilingual, strong on code), served by Ollama through the OpenAI-compatible provider; defaults
   and compose in section 9 (**Default embedding model**). Measured on the code suite
   (`eval/code/baseline-qwen3.json`, `simple` lexical leg, no reranker), recall at 10 and MRR:

   | Mode | All | Russian | Cross-language |
   |---|---|---|---|
   | dense (75, 34 and 24 questions) | 0.99 / 0.73 | 1.00 / 0.66 | 0.98 / 0.69 |
   | hybrid (96, 39 and 24) | 0.86 / 0.61 | 0.91 / 0.58 | 0.65 / 0.27 |
   | lexical (96, 39 and 24) | 0.61 / 0.42 | 0.64 / 0.47 | 0.06 / 0.02 |

   Hybrid and lexical are also asked 21 questions that dense is not: 17 about symbols, 4 quoting a
   message string. On the 75 questions of dense, hybrid scores 0.82 / 0.53 and lexical 0.54 / 0.32.
   Hybrid is below dense because the `simple` lexical leg is weak, across languages above all, and
   rank fusion weighs its top ranks, mostly wrong, as much as those of the dense leg. Improving the
   lexical leg is open question 1.

   The query instruction, measured in dense mode (R@1, R@5, R@10, MRR; then MRR of the Russian and
   the cross-language questions):

   | Task in `Instruct: {task}\nQuery:` | R@1 | R@5 | R@10 | MRR | ru | cross |
   |---|---|---|---|---|---|---|
   | none (no prefix) | 0.54 | 0.95 | 0.98 | 0.719 | 0.705 | 0.677 |
   | Given a web search query, retrieve relevant passages that answer the query (the model card's) | 0.52 | 0.91 | 0.99 | 0.700 | 0.656 | 0.639 |
   | Given a question about a software project, retrieve the code or documentation that answers it | 0.56 | 0.93 | 0.99 | 0.719 | 0.647 | 0.710 |
   | **Given a search query, retrieve the relevant code or documentation that can help answer the query** | 0.56 | 0.97 | 0.99 | 0.726 | 0.665 | 0.690 |

   Five other wordings (naming both languages, asking for snippets, shorter) scored MRR 0.684 to
   0.714. The chosen task is the best overall, but only by a question or two over none: every
   instruction helped the English questions (MRR 0.731 without, up to 0.78 with) and cost the
   Russian ones (0.705 without, 0.61 to 0.67 with), though with the chosen one every Russian answer
   is within the first 5. Measure again when the suite grows.
3. Settled on 2026-11-18 by the owner: Postgres with pgvector is the only store, the vector database
   included; no SQLite and no separate vector database such as Qdrant (see prior-art.md, Storage). It
   runs as its own compose service from the official pgvector image, pinned by digest: it upgrades
   and backs up on its own, and the service image stays one process.
4. Settled on 2026-11-18: source configuration lives in the database, managed through the HTTP API and
   the CLI; an apply-from-file command is optional and not built (section 6).
5. Smallest local model that extracts decisions well enough. Decided by the decision eval.
6. Settled on 2026-11-18: the license is Apache-2.0.
7. Default retention for messages and meetings.
8. Name collision. On 2026-11-18 `memini` was free on PyPI, while npm `memini` and the GitHub repository
   `eleboucher/memini` were small active projects in the same niche (memory for AI agents). Reserve the
   PyPI name before the first public push, or rename.
