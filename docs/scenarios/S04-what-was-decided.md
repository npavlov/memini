# S4. What was decided about X?

| Area | Who | Status | Issue |
|---|---|---|---|
| Decisions | Team lead | works | none |

## Who and pain

A decision is buried in a thread or a meeting, and a later one may have replaced it.

## Question

"What did we decide about archive retention, who agreed, and what did it replace?"

## Answer

`list_decisions` with `query`, `persons`, `since`, `until`; active items by default, history on
request. `get_decision` gives the evidence and the chain of replacements. `search` adds active
decisions to its hits.

## Data

Threads and meetings are pushed from outside (`memini push`, the HTTP API, an agent, or an MCP
recipe). The extraction job reads each new or changed record with the LLM its source allows and
writes journal items with evidence; a second check links an item to the one it replaces.

## Check

The decision eval (`eval/decisions`) and the supersession fixture.

## Notes

A `query` matches the words of a statement, not its meaning.

## See also

- [All scenarios](README.md)
- [S5](S05-latest-decisions-of-a-domain.md): What are the latest decisions about domain X?
- Tool reference: [list_decisions](../mcp/list_decisions.md)
- Tool reference: [get_decision](../mcp/get_decision.md)
- Tool reference: [search](../mcp/search.md)
