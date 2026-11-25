# S10. Find a definition in my language

| Area | Who | Status | Issue |
|---|---|---|---|
| Code | Engineer on another stack | partial | memini-qpk.2 |

## Who and pain

A repository in a language outside the five that are parsed is cut into line windows, and
`get_symbol` does not see it.

## Question

"Where is `BerthAllocator` defined?" in a Java or a Rust repository.

## Answer

`get_symbol` and definition-sized hits of `search_code` for the language.

## Data

Git sync as in S1. A classifier per language tells the chunker where definitions begin and end.

## Check

A chunking fixture per language; questions for new languages in the code eval.

## Notes

No work planned: the issue is deferred. Code in any language is already indexed as line windows,
searched, and attached to domains by its path; only definition-sized chunks and `get_symbol` are
limited to five languages. The owner sees no pain here now (2026-11-23). Before reopening, measure
the code eval with window-only chunks.

## See also

- [All scenarios](README.md)
- [S1](S01-where-is-x-implemented.md): Where and how is X implemented?
- Tool reference: [get_symbol](../mcp/get_symbol.md)
- Tool reference: [search_code](../mcp/search_code.md)
