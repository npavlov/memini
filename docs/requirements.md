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

Mirrors the MCP tools and adds administration: workspaces, sources, streams, runs, ingest, recipe plans,
persons and aliases, reindex, forget (hard delete of a record and everything derived from it).

**Source administration** (decided 2026-11-18, open question 4). Source configuration lives in the
database and is managed through the HTTP API and the CLI; there is no configuration file applied on
start. An apply-from-file command can come later on top of the same calls; it is not built. The routes
are under `/v1`: create a workspace; list, add, read, change and delete the sources of a workspace;
list the streams of a source with their lag, last run and queued sync; trigger a sync; list the runs of
a workspace, a source or a stream, newest first and paged; read one run with its report. Reads need
the `read` scope, changes the `admin` scope. A git source is a location, the refs it tracks and path
globs: `source.config` holds `{"location", "include", "exclude"}`, each tracked ref is a stream keyed
by its full name (`refs/heads/main`), and the schedule is set on all its streams at once. The location
is checked when the source is added, by the rules the mirror enforces at every fetch (section 9, git
mirrors), so a bad one is refused at once with a message that never repeats it; it never holds a
credential, and it cannot change: another repository is another source. A changed schedule starts
now. A triggered sync enqueues one job per stream with the stream's dedupe key, so it merges with a
scheduled one. Deleting a source deletes its streams, runs, jobs and records and removes its mirror in
the same request, under the source's job lock; while a job of the source runs, the deletion is refused
and can be retried.

## 7. Service and deployment

- One process: HTTP API, MCP over streamable HTTP, scheduler and workers. One Postgres database.
- `docker compose up` is the supported install. Binds to localhost by default.
- A CLI wraps the API for setup and operations. Setup (migrations, workspaces, tokens) works on the
  database directly: the first token cannot come from an API that asks for one.
- Secrets come from the environment or a secrets file, never from the database in clear text.
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
| Embeddings | Local, in-process or through an OpenAI-compatible endpoint (Ollama) | Open question 2 |
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
Which model ships as the default, and through which provider, is still open question 2.

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
embeds nothing and changes no row. The vector column has no dimension, so a change of model needs
no migration. The HNSW index is one partial index per embedding key on a cast of the column
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
hash is the blob id), other records from the stored payload.

**Code sync** (decided 2026-11-18). Each tracked ref of a git source is a stream; its pin, kept in
`stream.cursor`, is the commit its records are complete at and a digest of the rules that selected
them. A sync (the `git_sync` job, one stream at a time) fetches the mirror, then diffs the pin
against the commit the ref names now and writes only the files that differ; with no pin, a pin no
longer in the mirror (a force push, then gc), changed rules, or after a sync that failed part way,
it lists the ref in full and compares every file with its stored record instead. A file whose record
already holds its blob id is not read, chunked or embedded. A file's record is one file on one
ref: a Markdown file (`.md`, `.markdown`, `.mdx`, in any case) is a `document`, chunked by
headings, any other file a `code_file`; neither keeps a payload. Its external id is
`refs/heads/main:src/app.py`, its content hash the blob id, its time the committer time of the
commit it was written at. The kind is part of the record id and of the rules digest: when the
kinds change, the next sync lists the ref, writes each file as a record of its new kind and turns
its record of the old kind into a tombstone. A file is indexed when it is a regular file (not a
symlink or submodule) with a UTF-8 path, the source's globs select it, no built-in exclusion matches
it (lock files, minified bundles, source maps, `node_modules`; generated, large and of no use to
search), it is at most `git_max_blob_size` and it is not binary. Every other file, and a deleted
one, turns a live record into a tombstone whose chunks are deleted in the same transaction; a file
that comes back revives its record. Files are written in batches, each in one transaction with its
records and chunks, the last together with the tombstones and the move of the pin and
`checked_until` (the database time before the fetch), so a small change is all or nothing (principle
7). A larger one commits batch by batch and marks the pin partial first: a failure leaves the old
pin, every record agrees with its chunks, and the next sync lists the ref in full and embeds nothing
twice. One big transaction was not chosen: a first sync embeds for minutes to hours, and a failure
at its end would throw all of that away. A stale index blocks the sync: after the fetch, a workspace
whose fingerprint differs refuses it with the message that names `memini reindex`, no record
changes and the pin stays; the rebuild reads code from the fetched mirror, and the next sync
catches up from the pin. Writing records without their chunks meanwhile was not chosen: a sync
writes only what changed, so records stored during a rebuild, or before embeddings were set, would
stay out of the index. For the same reason `serve` syncs git sources only when an embedding provider
is configured. A ref no longer tracked loses its stream; the next sync of the source turns its
records into tombstones.

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
4. Settled on 2026-11-18: source configuration lives in the database, managed through the HTTP API and
   the CLI; an apply-from-file command is optional and not built (section 6).
5. Smallest local model that extracts decisions well enough. Decided by the decision eval.
6. Settled on 2026-11-18: the license is Apache-2.0.
7. Default retention for messages and meetings.
8. Name collision. On 2026-11-18 `memini` was free on PyPI, while npm `memini` and the GitHub repository
   `eleboucher/memini` were small active projects in the same niche (memory for AI agents). Reserve the
   PyPI name before the first public push, or rename.
