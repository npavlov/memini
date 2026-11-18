# Memini: implementation plan (draft v0)

Milestones in build order. Each ends in something that runs and can be checked. Requirements:
[requirements.md](requirements.md).

## Tracking

Tasks live in beads, one epic per milestone plus two tracks that run alongside: "CI/CD on GitHub" and
"Production hardening". `bd ready` lists what can start now; `bd show <id>` gives the description and the
acceptance check. The release task `Release v0.1.0` depends on every production gate.

## M0. Skeleton

- Repository, license, README, CI (lint, tests).
- `docker compose`: service and Postgres with pgvector.
- Migrations: workspace, principal, token, source, stream, record, run.
- Config, bearer-token auth, health endpoint.
- MCP endpoint with one tool: `status`.

Done when: `docker compose up` starts, an MCP client connects with a token and `status` answers.

## M1. Code search

- `git` connector: bare mirror, scheduled fetch, pinned commit per tracked ref, changed files from the diff.
- tree-sitter chunking and symbol table; line-window fallback.
- Local embeddings, lexical index, trigram index, hybrid search with rank fusion, optional rerank.
- Index fingerprint and rebuild.
- Tools: `search_code`, `read_source`, `get_symbol`, `search`.
- Retrieval eval harness with a code question set.

Done when: two repositories in two workspaces are indexed; a new commit re-chunks only changed files; a
rebuild from the mirrors gives the same chunk ids; the eval runs.

## M2. Ingest contract, messages and meetings

- Push API: record batches, window claims, idempotency, tombstones.
- Record kinds `thread`, `meeting`, `document`; chunking for each.
- Persons, aliases, "me"; deterministic resolution; manual merge.
- Filters, redaction, retention.
- Tools: `list_meetings`, `get_meeting`, `list_messages`, `get_thread`, `add_document`; `search` covers
  all kinds.

Done when: synthetic chat and meeting fixtures ingest twice with no change on the second run; "meetings I
attended about X" and "messages to me about X" return the expected records; expired records and their
chunks disappear.

## M3. MCP recipes

- Recipe format, parser interface, completeness checks, raw response store and replay.
- Agent executor: plan and next-call API, a Claude Code skill and a capture hook that post raw responses.
- Service executor: MCP client, scheduler, encrypted token storage, per-source locks.
- Stream health in `status`; run reports.
- Any source (requirements section 4): a declarative mapping parser, `memini recipe check` against a
  live server and a recipe-drafting skill, a catalog of bundled recipes for common chat, mail and
  note-taker servers, an IMAP connector, and `memini import` of export files.

Done when: one recipe run by both executors yields identical records; an incomplete window leaves the
checkpoint in place and lists the missing calls; a parser fix replays from stored raw responses.

## M4. Decision journal

- Extraction with structured output; `llm_policy` per source; provider config per workspace.
- Supersession check against similar active items.
- Tools: `list_decisions`, `get_decision`, `record_decision`, `supersede_decision`, `close_decision`,
  `retract_decision`.
- Decision eval on labelled synthetic threads.

Done when: on a fixture where thread B reverses thread A, A becomes `superseded`, the default list shows
only B and the history shows the chain with evidence links; the eval reports precision, recall and
supersession accuracy for the chosen local model.

## M5. Release

- Install guide, configuration reference, recipe authoring guide.
- Backup and restore, `forget`, reindex.
- Freshness gate: one call that says whether every stream and index is current.
- First tagged release.

Done when: a clean machine goes from clone to a working search over one repository and one pushed thread by
following the guide only.

## Order and risks

- M3 comes right after M2 because the agent executor is the only way to reach sources that have no
  headless token. Without it there is no real message data to test M4 on.
- Biggest risk: quality of decision extraction on local models. M4 starts with the eval, not with the
  pipeline.
- Second risk: MCP servers change their output shape. Raw responses are stored verbatim so parsers can be
  fixed and replayed.
- Open questions 1–3 of the requirements are settled in M1 by measurement on the eval set.
