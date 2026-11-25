# S10. Find a definition in my language

| Area | Who | Status | Issue |
|---|---|---|---|
| Code | Engineer on another stack | ready | memini-qpk.2 |

## Who and pain

A repository in a language without a grammar is cut into line windows, and `get_symbol` does not
see it. A team with Java, C# or Rust services could find their code by words only.

## Question

"Where is `BerthAllocator` defined?" in a Java or a Rust repository.

## Answer

`get_symbol` and definition-sized hits of `search_code` for the language.

## Data

Git sync as in S1. A classifier per language tells the chunker where definitions begin and end.
Twenty languages are parsed: Python, JavaScript, TypeScript, Ruby, Go, Java, C#, C, C++, PHP,
Kotlin, Swift, Rust, Scala, Dart, Lua, shell, SQL, Objective-C and Elixir. A method added to a type
outside it (a Rust `impl`, a Swift `extension`, `Dock::moor` in C++) is named after the type.

## Check

- A chunking fixture per language, with its golden file and the symbols it must define
  (`tests/test_chunking.py`).
- In the code eval, the repository `polyglot` and a symbol question per language: recall at 1 is
  1.00 in symbol mode for each of the fifteen (`eval/code/baseline-fake.json`).

## Notes

Other languages stay line windows: searched, and attached to domains by their path. R has no grammar
package with wheels, so it is not parsed. An upgrade to chunker 3 takes `memini reindex` for each
workspace; only the files of the new languages are embedded again.

## See also

- [All scenarios](README.md)
- [S1](S01-where-is-x-implemented.md): Where and how is X implemented?
- Tool reference: [get_symbol](../mcp/get_symbol.md)
- Tool reference: [search_code](../mcp/search_code.md)
