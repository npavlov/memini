# `get_symbol`

Find where a symbol is defined in code: by name (on) or qualified name (Lamp.on: names from the outermost definition, joined by '.' in every language), whole or as a prefix. Each definition carries its kind, parent, location as repo@sha:path#Lstart-Lend, ref, commit, occurred_at (the commit's time), the code that defines it as snippet, and context: the project, service and domains its file belongs to by the catalog. Filters: repositories, workspaces, paths, languages.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `name` | yes | A name (on) or, with a '.', a qualified name (Lamp.on): names from the outermost definition, joined by '.' in every language. |
| `match` | no | exact: the whole name; prefix: names that start with it. |
| `case_sensitive` | no | Whether case matters. |
| `limit` | no | The most results, 1 to 100. |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `repositories` | no | Only these repositories (git sources), by name. |
| `paths` | no | Only paths that match one of these globs, with the rules of .gitignore: * and ? within a directory, ** across directories (src/**/*.py); a glob without / matches at any depth (*.py); a directory matches everything below it (src/memini). |
| `languages` | no | Only code parsed as one of these languages. |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the `sensitive` scope; without it the call is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_symbol",
  "arguments": {
    "name": "hold_until"
  }
}
```

[MCP client setup and tool reference](README.md)
