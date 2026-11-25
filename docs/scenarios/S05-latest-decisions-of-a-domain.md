# S5. What are the latest decisions about domain X?

| Area | Who | Status | Issue |
|---|---|---|---|
| Decisions | Team lead | planned | memini-qpk.6 |

## Who and pain

A team lead wants the current decisions of one business area, newest first. Without a domain
filter the answer is the best matches by words, across everything.

## Question

"What are the five latest decisions about payments?"

## Answer

`list_decisions(domains=[...], limit=5)` without a query: newest first, complete for the decisions
that have a domain. `get_domain` includes the domain's active decisions. Each decision carries
`context` with its project and domains and how each came (`push`, `default`, `manual`, `code`,
`model`). The answer counts the project's decisions that have no domain (`without_domain`), so the
caller knows the list may be incomplete. `search(query, domains=[...])` answers by relevance
instead: its hits are the domain's code and the threads, meetings and pushed documents that belong
to it (pushed with it, a source or stream default, or else the model's domains of the decisions
taken from them), and its decisions are those of the domain, by statement words first, then those
citing the records it retrieved.

## Data

Decisions as in S4, plus a project and domains for each. One mailbox or chat holds messages of
several projects, so they are set per record (a thread, a meeting), not per source, and a decision
inherits them from the record its evidence is in. They arrive, in this order: whoever pushes the
record gives them in it; a source or a stream may carry a default; whoever records a decision with
`record_decision` gives `domains`, or links it later; code evidence is matched against the path
masks; otherwise the `assign_domains` job has a model read the statement and the cited messages and
pick from the catalog of the workspace. A project or domain that already came is never changed by
the model: a person or a stronger model set it. Mail of two companies in one mailbox is split by
whoever pushes it: a workspace is the isolation boundary, and nothing inside Memini routes across
it.

## Check

`tests/test_decision_domains_db.py` for the filters, the order, the count, the links, the
decisions of `search` by domain over HTTP and MCP, the threads `search` finds by a domain (pushed
with it, a subdomain, the model's), and the job on a fake model; `eval/domains` for
`assign_domains`: a labelled synthetic set that reports accuracy and how often the model answers
"none", against a baseline.

## Notes

The model step chose the right domains for 24 of 26 labelled items with `qwen3:4b-instruct` and
prompt `domains-v1` (`eval/domains/baseline-qwen3.json`); its errors give a domain to an item that
has none. A domain named with one of its own subdomains is kept as the subdomain alone. The set
holds two Russian items only.

## See also

- [All scenarios](README.md)
- [S4](S04-what-was-decided.md): What was decided about X?
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
- Tool reference: [list_decisions](../mcp/list_decisions.md)
- Tool reference: [record_decision](../mcp/record_decision.md)
