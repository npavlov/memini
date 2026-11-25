# S6. Which meetings did I attend, and what was sent to me?

| Area | Who | Status | Issue |
|---|---|---|---|
| My context | Team lead | works | none |

## Who and pain

A team lead loses track of what was said to them and where they were present.

## Question

"Which meetings about the export did I attend? What was sent to me about it?"

## Answer

`list_meetings` (default person: me) and `list_messages` with `roles`, `query`, `since`, `until`,
`sources`; `get_meeting` and `get_thread` for the whole record.

## Data

Pushed meetings and threads. Ingest links each record to persons by email or chat id; the operator
marks one person as "me".

## Check

`eval/comms/labels/queries.json`.

## See also

- [All scenarios](README.md)
- [S4](S04-what-was-decided.md): What was decided about X?
- Tool reference: [list_meetings](../mcp/list_meetings.md)
- Tool reference: [list_messages](../mcp/list_messages.md)
- Tool reference: [get_meeting](../mcp/get_meeting.md)
- Tool reference: [get_thread](../mcp/get_thread.md)
