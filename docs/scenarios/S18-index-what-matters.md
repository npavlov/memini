# S18. Index only what matters, see it move, keep my computer usable

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator, engineer | partial | memini-qpk.20 |

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

- **Preview.** `POST /v1/workspaces/{workspace}/sources/{source}/preview` counts what a sync would
  index at a ref, from the source's mirror: files tracked and kept, by extension and by top-level
  folder, and why the rest is left out. The body tries other `include`, `exclude` and
  `builtin_excludes` without saving them. The Codebases tab shows it with presets that know the
  common languages: tests (Go `_test.go`, Python `test_*.py`, JS and TS `*.test.*` and `*.spec.*`,
  Ruby `spec/`, Java and Kotlin `src/test/`, C# `*.Tests/`, Rust and PHP `tests/`, Swift
  `*Tests/`), generated code and build output, data files and assets. Markdown and source code are
  kept by default; saving the scope is `PATCH` of the source, and the next sync lists the ref again.
- **Progress.** The run of a git sync carries `details.progress`: files to write, files read,
  chunked and embedded, files committed, the batch it is at, and when writing started. The run is
  read with `GET /v1/runs/{id}` while it runs, every few seconds. The Codebases tab draws a
  progress bar with "N of M files" and an estimate from the rate so far.
- **Resources.** The managed server's model runs with a CPU limit the operator chooses on the
  Server screen: half the cores by default, or a background mode of one or two. Syncs can be
  paused and resumed.

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

## See also

- [S17. Add a repository without a terminal](S17-add-a-repository.md)
- [S11. Run without a terminal](S11-run-without-a-terminal.md)
- [All scenarios](README.md)
