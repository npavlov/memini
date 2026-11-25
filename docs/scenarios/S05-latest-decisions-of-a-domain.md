# S5. What are the latest decisions about domain X?

| Area | Who | Status | Issue |
|---|---|---|---|
| Decisions | Team lead | partial | memini-qpk.6 |

## Who and pain

A team lead wants the current decisions of one business area, newest first. Today the answer is the
best matches by words, across everything.

## Question

"What are the five latest decisions about payments?"

## Answer

`list_decisions(domains=[...], limit=5)` without a query: newest first, complete for the decisions
that have a domain. `get_domain` includes the domain's active decisions. Each decision carries
`context` with its project and domains and how each link was made (`link`, `model`, `source`,
`code`). The answer counts the project's decisions that have no domain, so the caller knows the list
may be incomplete.

## Data

Decisions as in S4, plus a project and domains for each. One mailbox or chat holds messages of
several projects, so they are set per record (a thread, a meeting), not per source, and a decision
inherits them from the record its evidence is in. They arrive, in this order: whoever pushes the
record gives them in it; a source or a stream may carry a default; whoever records a decision with
`record_decision` gives `domains`; code evidence is matched against the path masks; otherwise the
`assign_domains` job has a model read the statement and the cited messages and pick from the catalog
of the workspace. A project or domain that already came is never changed by the model: a person or a
stronger model set it. Mail of two companies in one mailbox is split by whoever pushes it: a
workspace is the isolation boundary, and nothing inside Memini routes across it.

## Check

A fixture for the filters and the count; a labelled synthetic set for `assign_domains` that reports
accuracy and how often the model answers "none".

## Notes

Today S4 answers by words only, ranked by the match.

## See also

- [All scenarios](README.md)
- [S4](S04-what-was-decided.md): What was decided about X?
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
- Tool reference: [list_decisions](../mcp/list_decisions.md)
- Tool reference: [record_decision](../mcp/record_decision.md)
