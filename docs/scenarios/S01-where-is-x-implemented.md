# S1. Where and how is X implemented?

| Area | Who | Status | Issue |
|---|---|---|---|
| Code | Engineer | works | none |

## Who and pain

An engineer needs code that lives in several repositories, not all of them checked out locally.

## Question

"Where do we compute the night surcharge?"

## Answer

`search_code` by meaning, by a substring or by a regular expression; filters `repositories`,
`paths`, `languages`. `get_symbol` for a definition by name. `read_source` to open the file at the
commit of the hit. Each hit carries a locator `repo@sha:path#Lstart-Lend` and the text of the chunk.

## Data

The operator registers git sources. `git_sync` keeps a bare mirror, pins a commit per tracked ref
and indexes only changed files. Nobody pushes code.

## Check

The code retrieval eval (`eval/code/suite.toml`, 101 questions) against its baseline.

## Notes

Definitions are recognised for twenty languages; see S10.

## See also

- [All scenarios](README.md)
- [S10](S10-definitions-in-my-language.md): Find a definition in my language
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
- Tool reference: [search_code](../mcp/search_code.md)
- Tool reference: [get_symbol](../mcp/get_symbol.md)
- Tool reference: [read_source](../mcp/read_source.md)
