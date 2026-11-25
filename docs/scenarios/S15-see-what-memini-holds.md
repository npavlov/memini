# S15. See what Memini holds and try its tools

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator, team lead | ready | memini-qpk.9 |

## Who and pain

The operator and the team lead see the base only through the answers of an AI agent. When an answer
is wrong they cannot tell missing data from a tool that did not find it.

## Question

"What is in the base, and what does `search` return for this query?"

## Answer

The Data screen of the desktop app, in two parts. Both only read.

- **What the base holds.** Per workspace: its records and chunks, and each source with its kind,
  streams, records, records not indexed, schedule and freshness. It reads `GET /v1/status` and
  `GET /v1/workspaces/{workspace}/sources`, so a source without a stream is listed too. Chunks are
  counted per workspace: no route counts them per source.
- **The read tools, by hand.** `search`, `search_code`, `get_symbol`, `read_source`,
  `list_decisions`, `get_decision`, `list_meetings`, `get_meeting`, `list_messages`, `get_thread`,
  and `list_commits` once the server has it. The form of a tool is built from the arguments the
  server publishes in `GET /openapi.json`, so a new filter appears without a change of the app, and
  a tool the server does not have is not offered. The answer is shown as cards and as the JSON the
  server sent, with the request that produced it. An argument the server refuses shows the server's
  error.

There is no write tool and no SQL: SQL would bypass the authorization filter. The app sends `GET`
to the routes of the read tools and nothing else, and shows what comes back as text.

## Data

None of its own. It reads what the other scenarios put into the base, through the routes that
exist, with a token that has the `read` scope.

## Check

Against a server with the synthetic fixtures: the overview lists each workspace's sources with their
counts; each read tool runs from its form and shows the same answer as its HTTP route; an argument
the server refuses shows the server's error; a server without `list_commits` does not offer it;
nothing in the screen can write.

## Notes

To confirm: whether the overview needs chunks per source, which would be a new field of the status
route.

## See also

- [All scenarios](README.md)
- [S11](S11-run-without-a-terminal.md): Run Memini without a terminal
- Tool reference: [the read tools](../mcp/README.md#tool-reference)
