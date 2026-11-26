# S18. Index only what matters, see it move, keep my computer usable

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator, engineer | ready | memini-qpk.20 |

## Who and pain

An engineer added a large repository from the desktop app. The local model took every core of the
laptop for an hour, the app said only "syncing", and nothing told how far it was or when it would
end. Afterwards a third of what was indexed were tests, fixtures and data files, which answer no
question about the business the code serves. The code bases are in many languages and several
repositories.

## Question

"How many files of harbour-api will be indexed if I leave out tests and fixtures? How far is the
sync, and how long is left? Can it run without taking the whole machine?"

## Answer

- **Preview.** `GET /v1/workspaces/{workspace}/sources/{source}/preview` counts what a sync would
  index at a ref, from the source's mirror: files tracked and kept, by extension and by top-level
  folder, and why the rest is left out. Its query tries other `include`, `exclude` and
  `builtin_excludes` without saving them. The Codebases tab shows it with presets that know the
  common languages: tests (Go `_test.go`, Python `test_*.py`, JS and TS `*.test.*` and `*.spec.*`,
  Ruby `spec/`, Java and Kotlin `src/test/`, C# `*.Tests/`, Rust and PHP `tests/`, Swift
  `*Tests/`), generated code and build output, data files and assets. Markdown and source code are
  kept by default; saving the scope is `PATCH` of the source, and the next sync lists the ref again.
- **Progress.** The run of a git sync carries `details.progress`: files to write, files read,
  chunked and embedded, files committed, the batch it is at, and when writing started. The run is
  read with `GET /v1/runs/{id}` while it runs, every few seconds. The Codebases tab draws a
  progress bar with "N of M files" and an estimate from the rate so far.
- **Index state, not last-run progress** (memini-qpk.34). A diff that changed nothing writes no
  progress, so a card that drew the last run's bar showed 0%. The card shows the state of the
  index instead: from the stream's `index` in `GET …/sources/{source}/streams`, the files and
  chunks it holds, the commit and its date. Under it, the last check from the run's counts:
  "07:37 — no changes", "3 changed, 1 deleted", or "full listing: N files written". While a sync
  runs, the bar is labelled **full listing** (the first sync, or one after a change of scope:
  every file of the branch) or **diff** (only what changed since the indexed commit; text that did
  not change keeps its vectors), from the run's steps `list` and `diff`. That difference is said
  once above the cards.
- **Mapped or not.** `GET …/sources/{source}/coverage` says how much of a repository the project
  map names: the projects with a service or a domain there and when and by which token each was
  last written, the share of the indexed files some domain names, and the top-level folders of
  the rest. The card says "not mapped" or "mapped: 80% of 1,472 files are in a domain" with those
  folders. **Map the code bases to the business** explains the three steps (copy the task, paste
  it to an agent connected to Memini, check the Project map) and shows the task before it is
  copied.
- **Resources.** The managed server's model runs with a CPU limit the operator chooses on the
  Server screen: half the cores by default, or a background mode of one or two
  (`memini-server indexing`). A code base can be paused on the Codebases tab: its schedule is
  switched off, and Resume sets it back to every hour; a sync already running finishes.
- **Work kept.** A first sync commits every 30 seconds of writing, so a sync stopped by an edit
  or a restart keeps what it wrote, and the next one skips it.

## Data

The tree of each ref is in the source's mirror after its first fetch; the preview reads it there.
Progress is written by the sync itself into its run. The CPU limit is a setting of the managed
server, kept by the launcher in its Compose files.

## Check

- `tests/test_gitsync_preview.py`: counts by extension and folder, reasons, groups past the limit.
- `tests/test_api_sources_db.py`: the preview of a fetched local repository with its own rules and
  with rules tried, a missing ref, a refused pattern, a source before its first fetch.
- `tests/test_gitsync_db.py`: `details.progress` after a sync in one and in two batches, after a
  batch that rolled back, and when nothing is written.
- `tests/test_catalog_db.py`: the coverage of a repository before and after a project is written,
  of one a service covers in part, of one no service is in, and of a source out of reach; the
  `index` of the streams of synced repositories.
- `desktop/src/repositories/codebase-state.test.ts`: a diff with no changes, a diff with counts, a
  diff that fell back to a full listing, the index of a stream, the coverage in words.

## See also

- [S17. Add a repository without a terminal](S17-add-a-repository.md)
- [S11. Run without a terminal](S11-run-without-a-terminal.md)
- [All scenarios](README.md)
