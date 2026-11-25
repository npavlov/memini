# `search_code`

Search the code of the git repositories the token may read. Modes: semantic (the default: a question or identifiers, by meaning and by words), exact (a substring), regex (a Postgres regular expression, matched line by line like grep), symbol (definitions by name, e.g. on, or qualified name, e.g. Lamp.on). Filters: repositories, workspaces, paths (globs with .gitignore rules, e.g. src/**/*.py), languages (python, javascript, typescript, ruby, go, java, csharp, c, cpp, php, kotlin, swift, rust, scala, dart, lua, shell, sql, objc, elixir). case_sensitive applies to exact, regex and symbol. projects, domains (a domain includes its subdomains; one is domain/subdomain) and services narrow to code the catalog names, by its path masks as they are now; a name it does not hold is an error, get_catalog lists them. A hit of a file of a repository carries context: the project and service it is in and the domains that name it. Each hit carries its source kind, workspace and source, locator (repo@sha:path#Lstart-Lend for code; source:external_id#anchor otherwise), occurred_at (the record's time, which since and until filter on), and the chunk's text as snippet. Hits also carry the ref, the commit, the symbol, and for exact and regex the line of the first match. Read more of a file with read_source.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `query` | yes | semantic: a question or identifiers; exact: a substring; regex: a regular expression (Postgres syntax, matched line by line as grep does); symbol: a name (on) or a qualified name (Lamp.on). |
| `mode` | no | semantic (the default): by meaning and by words; exact: a substring; regex: a regular expression; symbol: definitions by name. |
| `limit` | no | The most results, 1 to 100. |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `repositories` | no | Only these repositories (git sources), by name. |
| `paths` | no | Only paths that match one of these globs, with the rules of .gitignore: * and ? within a directory, ** across directories (src/**/*.py); a glob without / matches at any depth (*.py); a directory matches everything below it (src/memini). |
| `languages` | no | Only code parsed as one of these languages. |
| `projects` | no | Only code of these projects of the catalog, by name: their services, and the code their domains name. With domains or services, the projects those names are looked up in. A name the catalog does not hold is an error; get_catalog lists the names. |
| `domains` | no | Only code of these domains of the catalog: 'domain', which includes its subdomains, or 'domain/subdomain'. Membership is computed from the catalog's path masks as they are now. |
| `services` | no | Only code of these services of the catalog, by name: a repository or a directory. |
| `case_sensitive` | no | Whether case matters (exact, regex and symbol; the other modes ignore case). |

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
