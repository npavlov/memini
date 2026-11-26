# `search_code`

Search the code of the repositories the token may read. Modes: semantic (the default: a question or identifiers, by meaning and by words), exact (a substring), regex (a Postgres regular expression, matched line by line like grep), symbol (definitions by name, e.g. on, or qualified name, e.g. Lamp.on: names from the outermost definition, joined by '.' in every language; whole names first), symbol_prefix (definitions whose name starts with it). Filters: workspaces, sources (repositories), paths (globs with .gitignore rules, e.g. src/**/*.py), languages (python, javascript, typescript, ruby, go, java, csharp, c, cpp, php, kotlin, swift, rust, scala, dart, lua, shell, sql, objc, elixir). case_sensitive applies to exact, regex, symbol and symbol_prefix. projects, domains (a domain includes its subdomains; one is domain/subdomain) and services narrow to code the catalog names, by its path masks as they are now; a name it does not hold is an error, get_catalog lists them. A hit of a file of a repository carries context: the project and service it is in and the domains that name it. Each hit carries its id (the chunk's), source kind, workspace and source, locator (repo@sha:path#Lstart-Lend for code; source:external_id#anchor otherwise), occurred_at (the record's time, which since and until filter on), and the chunk's text as snippet. left_out counts the hits of records marked sensitive that were left out of items. Hits also carry the ref, the commit, the symbol, for exact and regex the line of the first match, and for symbol and symbol_prefix the definition: its kind, parent and lines. Read more of a file with get_file.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |
| `query` | yes | semantic: a question or identifiers; exact: a substring; regex: a regular expression (Postgres syntax, matched line by line as grep does); symbol and symbol_prefix: a name (on) or a qualified name (Lamp.on): names from the outermost definition, joined by '.' in every language. |
| `mode` | no | semantic (the default): by meaning and by words; exact: a substring; regex: a regular expression; symbol: definitions with this name, whole names first, then the shorter qualified name; symbol_prefix: definitions whose name starts with it. |
| `limit` | no | The most results, 1 to 100. |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `sources` | no | Only these repositories (sources of kind repository), by name. |
| `paths` | no | Only paths that match one of these globs, with the rules of .gitignore: * and ? within a directory, ** across directories (src/**/*.py); a glob without / matches at any depth (*.py); a directory matches everything below it (src/memini). |
| `languages` | no | Only code parsed as one of these languages. |
| `projects` | no | Only code of these projects of the catalog, by name: their services, and the code their domains name. With domains or services, the projects those names are looked up in. A name the catalog does not hold is an error; get_catalog lists the names. |
| `domains` | no | Only code of these domains of the catalog: 'domain', which includes its subdomains, or 'domain/subdomain'. Membership is computed from the catalog's path masks as they are now. |
| `services` | no | Only code of these services of the catalog, by name: a repository or a directory. |
| `case_sensitive` | no | Whether case matters (exact, regex, symbol and symbol_prefix; the other modes ignore case). |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "search_code",
  "arguments": {
    "query": "hold_until",
    "mode": "exact"
  }
}
```

[MCP client setup and tool reference](README.md)
