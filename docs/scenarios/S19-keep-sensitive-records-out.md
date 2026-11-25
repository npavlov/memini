# S19. Keep what is private out of answers without deleting it

| Area | Who | Status | Issue |
|---|---|---|---|
| My context | Owner of the base | ready | memini-qpk.30 |

## Who and pain

Dana pushed her mail into Memini. Two threads hold a salary settlement and a date of birth. She
wants to keep them, because she may need them later, but no agent should quote them while it
answers about work, and no model should read them.

## Question

"Keep the thread about my final settlement, but do not show it to my agents unless I ask for it."

## Answer

A record can be marked sensitive. It stays stored and indexed; nothing is deleted or re-embedded.

- **Marking.** An admin marks or clears one record with
  `PUT /v1/workspaces/{workspace}/records/sensitive` (`memini sensitive mark|unmark`, or *Mark a
  record…* on the Data screen of the desktop app), named by `record_id` or by `source` and
  `external_id`, with an optional short reason. A push marks a record with `"sensitive": true`; a
  push never clears a mark. `GET` on the same route (`memini sensitive list`) lists the marked
  records with their reasons and no content.
- **Default.** Every read leaves a marked record out: `search` in every mode, `search_code`,
  `search_code` in mode `symbol`, `list_messages`, `list_meetings`, the decisions whose every piece of evidence is a
  marked record, and the export. `get_thread` and `get_meeting` answer 404 `sensitive_record`, so the
  reader knows it is there. A decision with other evidence too shows the marked one as
  `record_state: sensitive`, with its reference only.
- **Asking.** `include_sensitive=true` on those reads returns them, only for a token with the new
  scope `sensitive`; any other token is refused (403 `insufficient_scope`, or a tool error).
- **Models.** No extraction or domain step reads a marked record, whatever the token.

## Data

The records are pushed as usual. The mark is a column of the record (migration 0022), set by the
push or by an admin; the reason is redacted like any text an agent writes.

## Check

`tests/test_sensitive_db.py` on synthetic threads: a push marks and never clears; an admin marks,
clears and lists; search over HTTP and MCP leaves the record out, refuses `include_sensitive`
without the scope and returns it with the scope; `get_thread` names it as sensitive; a decision
whose only evidence it is disappears; no extraction job is queued for it; the export leaves it out
unless asked. `tests/test_auth.py` checks the read filter itself. The retrieval eval is unchanged.

## See also

- [All scenarios](README.md)
- [S6. Which meetings did I attend, and what was sent to me?](S06-my-meetings-and-messages.md)
