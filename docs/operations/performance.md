# Performance

How fast Memini indexes and searches, what it needs, how to measure it again, and what was tuned.
The numbers come from `memini bench` ([requirements.md](../requirements.md), section 10,
Performance) on the reference machine below. The JSON reports of the runs quoted are in
[bench/](bench/); each table names its run.

## Run the benchmark

```sh
export MEMINI_DATABASE_URL=postgresql://memini:memini@127.0.0.1:55432/memini   # any server; CREATEDB
uv run memini bench                       # the default run: about 25 minutes, no model needed
uv run memini bench --json bench.json     # the report as JSON as well
make bench BENCH_ARGS="--json bench.json" # the same through make
```

`memini bench` is an operator command, like `memini eval`: it creates a scratch database
(`memini_bench_<random>`) on the server `MEMINI_DATABASE_URL` names, builds everything there
through the service's own code, and drops it at the end (`--keep` keeps it). The configured
database itself is not read or written; the role needs `CREATEDB`. It is not part of `make check`
or CI, which run a smoke test of it instead (`tests/test_bench_db.py`: a three-file repository,
eight records, every step). Give it a Postgres of its own when the numbers matter: other work on
the same server or machine shows in them.

What it does, in order (`src/memini/bench/runner.py` has the details):

1. **Repository.** `--repo` is a local git repository or an https URL; the default is Django
   (`https://github.com/django/django.git`, BSD-3-Clause) at the tag `6.1.3`. A URL is cloned once
   into `~/.cache/memini/bench` (bare, one commit, with the service's git hardening: no user config,
   no credential helper, no hooks; nothing in it is run) and reused; its tree is committed again as
   a commit without parents, because a mirror does not fetch from a shallow clone. `--include` and
   `--exclude` are the source's globs.
2. **Indexing.** The mirror is created from it; every selected file is read from the mirror and
   chunked in a pass of its own (`read`, `chunk`); then the service's git sync
   (`memini.gitsync.run_git_sync`) reads, chunks, embeds and writes it all (`git sync`). Then the
   synthetic message set, `--threads` threads and `--meetings` meetings (default 5,000 and 1,000;
   invented people, `example.test` addresses, a fixed seed), goes through `memini.ingest.ingest`
   with the service's indexing hook in batches of 200 (`messages`). Embedding time is measured
   around the embedder; `write` is the wall time less embedding, reading and chunking: the
   database writes (rows, the lexical column, the trigram, GIN and HNSW indexes) and the
   bookkeeping around them.
3. **Embedder.** The index is built with a fake embedder of 1024 dimensions, the default model's,
   unless `--embedder configured`: the real model takes most of an hour on this corpus, and
   nothing but dense recall depends on what the vectors mean. `--embedding-sample N` times the
   configured model apart, on N of the corpus's chunk texts at each `--embedding-batch-sizes` and
   on single queries, and extrapolates to the whole corpus.
4. **Sizes** after `VACUUM ANALYZE`: the database, the `chunk` table and each of its indexes.
5. **Search.** 200 queries per mode, made from the index (definition names for `symbol`, `exact`
   and `regex`; their words and the topics of the message set for the ranked modes), through
   `memini.search.search` as the API calls it, one pooled connection per search, at each
   `--concurrency` (default 1 and 8), after one untimed pass of every query (a first pass right
   after indexing is several times slower: the cache is cold). Hybrid also at each `--candidates`.
   The HTTP and MCP layers add their own few milliseconds; the dense and hybrid figures of a fake
   run have no model call in them (add the query's embedding, below).
6. **Dense retriever.** The dense statement against the exact nearest chunks (the same statement
   with index scans off), unfiltered and filtered to messages, at each `--ef-search`: recall at 10
   and at 50 (the hybrid candidates), and the statement's latency; on the index the service built
   row by row, then that index rebuilt with `CREATE INDEX` (timed: what a restore pays), then each
   `--hnsw m:ef_construction[:halfvec]` built at `--maintenance-work-mem` (default 1GB). Recall
   counts a returned chunk that is as near as the exact k-th one (near-duplicate texts tie, and
   which of them an exact sort keeps is arbitrary); the report also gives the share of exact ids.
7. **Memory**: the process's peak resident memory after each step.

## The reference machine and runs

Apple M4 Max (12 performance and 4 efficiency cores), 48 GB, macOS 27.2; Docker (OrbStack, 16
CPUs and 25 GiB for its VM). Postgres 18.7 with pgvector 0.8.7, the image of `compose.yaml`, with
the image's settings (`shared_buffers` 128MB, `work_mem` 4MB, `maintenance_work_mem` 64MB, two
parallel workers per query), in a container of its own per run. Ollama 0.34.2 with
`qwen3-embedding:0.6b` (Q8_0, digest `ac6da0df…`), on the GPU. Python 3.12. Other work shared the
machine, as it does on a workstation: another process used Ollama (a 4B model) during the runs
with the real model, run A overlapped run R, and runs B and C overlapped each other.

| Run | What | Report |
|---|---|---|
| A | the default run, before the search fix below | [bench/a-before.json](bench/a-before.json) |
| B | the default run, the search fix in | [bench/b-default.json](bench/b-default.json) |
| C | the default run, its Postgres limited to 2 CPUs and 2 GB (`docker run --cpus 2 --memory 2g`) | [bench/c-small.json](bench/c-small.json) |
| R | the real model on part of Django and 1,200 records, all of the HNSW grid | [bench/r-real.json](bench/r-real.json) |
| R2 | the real model on Django's `db`, `core`, `forms`, `http`, `urls` and 250 records, the grid again with tie-aware recall | [bench/r2-real.json](bench/r2-real.json) |

The corpus of the default run: Django 6.1.3, 7,085 files, of which 7,074 selected (44.3 MiB: 5,683
code files, 9 Markdown documents and 1,382 binary files, `.mo` catalogues, found when read),
giving 43,788 chunks and 46,759 symbols; and 5,000 threads and 1,000 meetings, 33,296 chunks. In
all 11,692 records and **77,084 chunks**.

## Indexing

Run B, fake embedder (the time of the model is the next table):

| Step | Wall | Embedding | Read | Chunk | Writes | Chunks | Per chunk |
|---|---|---|---|---|---|---|---|
| mirror fetch (local) | 0.4 s | | | | | | |
| read and chunk, apart | 4.7 s | | 1.2 s | 3.5 s | | 43,788 | 0.1 ms |
| git sync, 5,692 files | 577 s | 8.4 s | 1.2 s | 3.5 s | 564 s | 43,788 | 12.9 ms |
| messages, 6,000 records | 491 s | 5.5 s | | 2.6 s | 482 s | 33,296 | 14.5 ms |

Reading and chunking cost almost nothing (7,800 chunks a second, tree-sitter included). With the
fake embedder the time is the database's: about 13 ms per chunk, most of it the insert into the
HNSW index of 1024 dimensions (Django's `db` package with vectors of 8 dimensions wrote 4.6 times
faster than with 1024),
growing slowly with the index (3.7 ms per chunk for the first 10,000 in run R). An earlier default
run alone on the machine wrote 9.1 and 13.2 ms per chunk (410 and 447 s); run C, with 2 CPUs,
12.6 and 14.7: a sync writes on one connection, so more cores do not make it faster.

**The model.** Qwen3-Embedding 0.6B through Ollama, on the reference machine's GPU:

| Measured | Texts a second | Note |
|---|---|---|
| Ollama alone, texts of 1,800 characters, batches of 1, 4, 8, 16, 32, 64 | 19.8, 21.1, 21.6, 21.6, 21.6, 21.5 | a probe before the runs |
| Ollama alone, batches of 8: texts of 240, 720, 1,800 characters | 47.9, 34.9, 21.3 | about 20 ms per text plus its length |
| the same, 4 requests at a time | 49.6, 34.8, 21.5 | Ollama runs them one after the other |
| run R, git sync (10,027 code chunks) and messages (6,762) | 12.0 and 5.5 | Ollama shared with a 4B model |
| run R sample, 500 chunk texts (980 characters), batches of 1, 8, 32, 64 | 10.0, 11.3, 1.8, 10.6 | shared; the 1.8 met the other model's load |
| one query, run R | p50 175 ms, p95 449 ms | shared; alone about 17 ms |

So the model sets the time of a first sync: at 20 texts a second the 77,084 chunks of the default
corpus take about 65 minutes of embedding against 18 of writes; shared with a generating model, as
in run R, three times as long. The batch size changes nothing with Ollama, which embeds the texts of
a batch, and concurrent requests, one after another: `embedding_batch_size` stays 32 (it matters
for a server that batches, such as vLLM or llama.cpp). A sync embeds the chunks of one file per
call, so batching across files would not help with Ollama either. The query of a hybrid or dense
search costs one call, about 17 ms when Ollama is idle.

An unchanged text is never embedded again (principle 6): a later sync embeds only what changed, a
`memini reindex` after a change of chunker almost nothing.

## Size

Run B, after `VACUUM ANALYZE`: the database is **1,407 MiB for 77,084 chunks, 18.7 KiB a chunk**.

| Part | MiB | Per chunk |
|---|---|---|
| `chunk`, rows and TOAST (content, header, vector, lexical column) | 529 | 7.0 KiB |
| HNSW index (`vector(1024)`, m 16, ef_construction 64) | 602 | 8.0 KiB |
| trigram index of the content | 67 | 0.9 KiB |
| GIN index of the lexical column | 22 | 0.3 KiB |
| other indexes of `chunk` | 49 | 0.6 KiB |
| `record` (payloads, normalized text), `symbol` and the rest | 138 | |

The HNSW index is the largest part: a 1024-dimensional `vector` is 4 KiB, so a page of 8 KiB holds
one element. On `halfvec` it is a third of that (below).

## Search

Run B, warm, 200 queries per mode, milliseconds end to end through `memini.search.search` (no
model call: add the query's embedding, about 17 ms, to dense and hybrid):

| Mode | p50 | p95 | p99 | Searches a second | at 8 at a time: p50 | p95 | p99 | a second |
|---|---|---|---|---|---|---|---|---|
| hybrid | 96 | 111 | 123 | 11.5 | 212 | 466 | 529 | 33 |
| dense | 11 | 17 | 24 | 82 | 28 | 34 | 36 | 281 |
| lexical | 85 | 97 | 100 | 13.6 | 211 | 503 | 553 | 35 |
| exact | 41 | 83 | 85 | 33 | 55 | 154 | 244 | 129 |
| regex | 72 | 154 | 178 | 13.6 | 132 | 509 | 567 | 44 |
| symbol | 3.6 | 5.4 | 7.4 | 266 | 13 | 21 | 25 | 557 |

The lexical retriever is most of a hybrid search. It looks for any word of the query and ranks
every chunk that holds one: a question of common words matches a fifth of the chunks (the
synthetic messages, written from a small vocabulary, are an extreme of that), which a parallel scan
ranks in about 100 ms. Its cost grows with the number of chunks that match, so with the corpus.

**Generic plans** (the fix between runs A and B). psycopg prepares a statement once a connection
has run it five times, and Postgres then keeps one plan for all values if it looks cheaper than
the plans made for the values so far. For the lexical statement it does: not knowing the words, it
expects a few hundred matches and walks the GIN index on one process. On this corpus that plan
took 630 ms where the plan for the words took 180 ms (one query, `EXPLAIN ANALYZE`), and in run A
most searches ran it, since a pooled connection serves many. Search now sets
`plan_cache_mode = force_custom_plan` in its transaction (`memini.search.engine`;
`tests/test_search_plans_db.py`):

| Run A → B (p50 / p95, ms) | concurrency 1 | concurrency 8 |
|---|---|---|
| hybrid | 654 / 1,210 → 96 / 111 | 469 / 1,282 → 212 / 466 |
| lexical | 606 / 1,208 → 85 / 97 | 399 / 1,179 → 211 / 503 |
| exact | 83 / 149 → 41 / 83 | 105 / 248 → 55 / 154 |
| regex | 172 / 274 → 72 / 154 | 214 / 575 → 132 / 509 |

**Candidates.** Hybrid with `search_candidates` 20, 50 and 100: p50 115, 96 and 116 ms, p95 174,
111 and 154 (run B, concurrency 1; the lexical leg ranks every match whatever the limit). Fewer
candidates save nothing here and cost recall on the eval (0.01, requirements section 9, Lexical
search), so 50 stays.

**With the model.** Run R (16,789 chunks, the real model, Ollama shared): hybrid p50 144 and p95
355 ms, dense 63 and 154 ms at concurrency 1, most of it the query's embedding (p50 175 ms while
Ollama was shared, about 17 ms alone).

**Reranker.** Off by default; when on, it adds about 0.5 s at depth 5 on this machine
(requirements section 9, Reranker).

**Retrieval eval.** `make eval` meets its baseline with the fix. `make eval-qwen3` gives the same
numbers with and without it (all questions, R@1 / R@5 / R@10 / MRR: hybrid 0.61 / 0.98 to 0.99 /
0.99 / 0.77, dense 0.56 / 0.97 / 0.99 / 0.73, lexical 0.52 / 0.79 / 0.82 / 0.66). Both runs exit 4
on one group, hybrid `tag:filtered` (3 questions, MRR 0.67 to 0.56), against
`eval/code/baseline-qwen3.json`, which its own notes say was measured with another chunker,
lexical retriever and fusion: that baseline needs writing again, apart from this change.

## The HNSW index

The service builds one HNSW index per embedding model with pgvector's defaults, `m` 16 and
`ef_construction` 64, and searches it with `hnsw.ef_search` = `max(search_candidates, limit)`, 50
by default, with the iterative scan for filters.

Recall against exact search needs vectors that mean something, so it was measured with the real
model: run R (16,789 chunks: 10,027 of Django's Python, 6,762 of 1,200 synthetic records) and run
R2 (5,851 chunks). The default run's figures (0.04 to 0.1) only show that random directions are the
worst case for HNSW. Run R, 200 queries, the share of the exact ten nearest that the dense
statement returned, and its latency at `ef_search` 50:

| Index | ef_search 10 | 20 | 40 | **50** | 100 | 200 | p50 at 50 |
|---|---|---|---|---|---|---|---|
| m 16, ef_construction 64, row by row (the service's) | 0.81 | 0.87 | 0.91 | **0.92** | 0.94 | 0.95 | 3.5 ms |
| the same, filtered to messages (iterative scan) | 0.70 | 0.82 | 0.88 | 0.90 | 0.93 | 0.95 | 6.5 ms |
| m 16, ef_construction 64, by `CREATE INDEX` | 0.77 | 0.84 | 0.88 | 0.88 | 0.91 | 0.94 | 3.0 ms |
| m 8, ef_construction 32 | 0.63 | 0.74 | 0.79 | 0.80 | 0.82 | 0.85 | 2.6 ms |
| m 16, ef_construction 32 | 0.57 | 0.63 | 0.68 | 0.69 | 0.74 | 0.78 | 3.3 ms |
| m 16, ef_construction 128 | 0.62 | 0.71 | 0.75 | 0.76 | 0.82 | 0.88 | 3.0 ms |
| m 24, ef_construction 64 | 0.81 | 0.88 | 0.91 | 0.92 | 0.93 | 0.95 | 3.6 ms |
| m 32, ef_construction 128 | 0.76 | 0.82 | 0.85 | 0.85 | 0.88 | 0.92 | 3.5 ms |
| m 16, ef_construction 64, on `halfvec` | 0.67 | 0.73 | 0.77 | 0.77 | 0.81 | 0.87 | 3.1 ms |

These are a floor, and noisy. Two fifths of run R's chunks are synthetic messages written from a
small vocabulary, whose vectors are nearly the same, so which ten of them are "the exact ten" is
close to arbitrary (a larger `ef_construction` scoring lower is that noise, not HNSW); each build of
the grid is one random graph. Run R2, mostly code, counts a returned chunk as near as the exact
tenth as found: there the planner sorted exactly (recall 1, 17 to 19 ms) wherever it estimated that
cheaper, which on 5,851 chunks was most settings, and where it scanned the index, m 16 and
ef_construction 64 found 0.97 at `ef_search` 200, m 24 0.96 at 100, m 32 and 128 0.97 at 100 and
0.99 at 200. A workspace of a few thousand chunks is searched exactly anyway.

So the service's settings stay: none of the grid beats m 16 and ef_construction 64 at the same
latency (m 24 matches it with a larger graph; the smaller settings lose 0.1 to 0.2), and an index
built row by row, as the service builds it, recalled more than one built at once (0.92 against
0.88). Raising `ef_search` from 50 to 100 would add 0.02 for half a millisecond; it follows
`search_candidates`, and hybrid search, which fuses the 50 with the lexical candidates, already
finds 0.99 at 10 on the retrieval eval, so it stays too.

**Builds.** The service builds its index row by row as chunks arrive (part of the writes above). A
restore (`pg_restore`) builds it at once, with the server's `maintenance_work_mem`:

| Build of the default corpus (77,084 vectors) | Time | Size |
|---|---|---|
| `vector`, `maintenance_work_mem` 64MB (the image's default) | 220 to 298 s | 602 MiB |
| `vector`, 1GB | 48 s | 602 MiB |
| `halfvec`, 1GB | 33 s | 201 MiB |

So a restore of a large database is several times faster with more `maintenance_work_mem` for
the session that restores it (`PGOPTIONS='-c maintenance_work_mem=1GB'`), when the machine has the
memory.

## Git sync batches

The git sync writes at most 100 files and 8 MiB in one transaction (`SyncLimits`). Django's 909
Python files (9,210 chunks), fake embedder, twice each, on run A's server:

| Files a transaction | git sync | Peak memory |
|---|---|---|
| 25 | 67.0 s, 68.0 s | 114, 121 MiB |
| 100 | 68.5 s, 69.8 s | 120, 121 MiB |
| 400 | 67.7 s, 72.1 s | 126, 131 MiB |

The batch does not move the time: each file is written by statements of its own and a commit is
cheap. A larger batch holds more in memory and loses more work on a failure, a smaller one moves the
`partial` mark more often; 100 stays.

## Targets

On the reference machine, with the default corpus (77,084 chunks), a warm cache and the image's
Postgres settings. `memini bench` checks them by eye: a change that misses one is a regression to
explain.

| What | Target | Measured (run B) |
|---|---|---|
| search p95, concurrency 1: symbol / dense / lexical / hybrid / exact / regex | 25 / 50 / 250 / 250 / 250 / 500 ms | 5 / 17 / 97 / 111 / 83 / 154 |
| search p95, concurrency 8, every mode | under 1 s | 21 to 509 ms |
| dense recall at 10 against exact search, real vectors, at the service's ef_search | at least 0.9 | 0.92 (run R) |
| database writes per chunk, fake embedder | under 20 ms | 12.9 and 14.5 |
| embedding with the default model, Ollama not shared | at least 20 texts a second | 21.6 (1,800 characters) |
| peak memory of the process while it indexes and searches | under 256 MiB | 160 MiB |
| database size per chunk at 1024 dimensions | under 24 KiB | 18.7 KiB |

Dense and hybrid with the real model add one query embedding (about 17 ms). The interactive bar
behind them: an MCP tool should answer within about a second, as for the reranker.

## Tuning knobs

| Setting | Default | What it trades |
|---|---|---|
| `MEMINI_SEARCH_CANDIDATES` | 50 | candidates of each hybrid retriever, and `hnsw.ef_search`: recall against latency (above) |
| `MEMINI_SEARCH_TIMEOUT` | 5 s | how long one search statement may run (a regex that backtracks) |
| `MEMINI_SEARCH_RERANKER` | off | +0.17 R@1 on the eval for about 0.5 s at p95 (requirements section 9) |
| `MEMINI_EMBEDDING_BATCH_SIZE` | 32 | texts per request; no effect with Ollama, which embeds them one by one |
| `MEMINI_EMBEDDING_TIMEOUT` | 60 s | per request; raise it for a slow CPU-only model server |
| Postgres `maintenance_work_mem` | 64MB | the time of an HNSW build in a restore or by hand: 1GB built the default corpus in 48 s, not 220 |
| Postgres `shared_buffers` | 128MB | the image's; the database's working set is cached by the OS anyway (below) |
| Ollama `OLLAMA_CONTEXT_LENGTH` | the model's 32,768 | the memory Ollama holds for the model: 3.8 GiB at the default; chunks need under 2,000 tokens |

Not settings: HNSW `m` and `ef_construction` (in `memini.index.vectors`, above), the git sync batch
(100 files and 8 MiB per transaction, `memini.gitsync.SyncLimits`), and the ingest batch (the
client's choice, at most `MEMINI_INGEST_MAX_RECORDS`).

## The smallest supported machine

**4 CPU cores, 8 GB of memory, 20 GB of free SSD**, x86-64 or arm64, for the default corpus's size
(one large repository and a team's messages, about 80,000 chunks), with Ollama on the same machine.

- **Postgres** ran the default corpus limited to 2 CPUs and 2 GB (run C): one search at a time
  stayed within the targets (p95: symbol 6, dense 14, lexical 171, hybrid 216, exact 120, regex
  239 ms), and its writes were as fast as unlimited. Eight at a time saturate two cores (hybrid p95
  1.7 s, regex 2.6 s), so two more cores are for several agents at once and for Ollama. Its
  container held up to 400 MiB in run C, and 800 MiB without limits under load (run A).
- **Memory**, for that corpus: Ollama with the model about 3.8 GiB at its default context
  (`ollama ps`; the weights are 0.64 GB; a smaller context holds less, unmeasured); Postgres up
  to 0.8 GiB, and the operating system's cache for the database's working set, the HNSW index
  (0.6 GiB) first; the service under 0.2 GiB (160 MiB at its peak in the benchmark, 76 MiB idle in
  compose); the system. About 6.5 GB, so 8 GB; 16 GB gives room for a larger corpus, the reranker
  (1.7 GB at its peak) or an LLM for decision extraction.
- **Disk**: the images (0.33 GB the service, 0.49 GB Postgres), the model (0.64 GB), the database
  at about 19 KiB a chunk (1.4 GB here) with its WAL, the mirrors (a bare clone of each
  repository with the history of its branches and tags, larger than its tree; Django's tree packs
  to 13 MB), and room for the backups the operator keeps. 20 GB on an SSD; an HNSW scan reads
  pages at random.
- **CPU and GPU.** Nothing needs a GPU, but the model sets the time of a first sync: on the
  reference machine's GPU about 20 chunks a second, so about an hour for this corpus. On its CPU
  alone (Ollama with `num_gpu` 0) it embedded 0.24 texts of 1,400 characters a second with 4
  threads (0.16 with 12, the machine busy): days for this corpus. That is not a measurement of a
  CPU server, but it says not to assume one is fast enough: measure the target machine with
  `memini bench --embedding-sample 200` (a provider configured), which times the model alone, and
  give a CPU-only machine a GPU, a smaller first corpus, or the time.

Below that: 2 cores and 4 GB run a small corpus (a few repositories of a few thousand files) with
Ollama's context lowered, unmeasured.

## Not measured, and next

- **The HNSW index on `halfvec`.** A third of the size (602 against 201 MiB for 77,084 vectors),
  a third faster to build (33 against 48 s at 1GB) and to scan (p50 4.9 against 6.6 ms, fake
  vectors); its recall was lower in run R (0.77 against 0.88 for the same build at `ef_search`
  50) and 0.96 to 0.97 in run R2, where the `vector` index was sorted exactly. Worth measuring on
  a larger real corpus with the tie-aware recall and the retrieval eval; adopting it changes the
  index expression (section 9 casts to `vector` up to 2000 dimensions), so each index is rebuilt.
- **`maintenance_work_mem` for restores**: `scripts/restore.sh` could pass a larger one to
  `pg_restore` (above); not changed, as it needs the memory at restore time.
- **Building the HNSW index after a first sync** instead of row by row: a `CREATE INDEX` at 1GB took
  48 s where the inserts cost most of 18 minutes of writes; but the model's hour dominates a
  first sync, and search would be exact until the build.
- **The lexical leg** grows with the chunks a query's words match; a bound on what it ranks
  (rank only chunks that match most words first, or the GIN index's own cut) is the next step for
  hybrid latency on larger corpora, measured with the retrieval eval.
- HTTP and MCP overhead, several workspaces and users, a CPU-only Ollama on a small machine with
  real workloads, Postgres settings other than the image's.
