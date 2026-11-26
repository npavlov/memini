# `get_file`

Read a file, a range of its lines, or a directory listing from a repository (source) the token may read. By default at the pinned commit of the repository's default tracked branch, the commit search results are at; ref takes a branch or tag name, a full ref or a full commit id. path is relative to the repository root (empty for the root); absolute paths, '..' and control characters are refused. Lines are 1-based and inclusive, at most 2000 lines and 100000 characters per call: when truncated, continue from end_line + 1. A binary file or one over the size limit returns no content; a symlink returns its target, not followed; a file marked sensitive is refused unless asked with include_sensitive. The result carries the commit, its time, a locator repo@sha:path#Lstart-Lend, the definitions in the lines read, and context: the project, service and domains the path belongs to by the catalog.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `source` | yes | The repository (a source of kind repository), by name. |
| `path` | no | A file or directory, relative to the repository root; empty for the root. |
| `workspace` | no | The repository's workspace; may be left out when only one workspace the token may read has a repository of that name. |
| `ref` | no | A branch or tag name (main, v1.0), a full ref (refs/heads/main), or a full commit id. Default: the repository's default tracked branch. A tracked ref is read at its pinned commit, the one search results are at; any other ref at the commit the mirror has for it. |
| `start_line` | no | First line to read, 1-based; default 1. |
| `end_line` | no | Last line to read, inclusive; default: as far as the limits allow. |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_file",
  "arguments": {
    "source": "harbour-code",
    "path": "src/berth_hold.py"
  }
}
```

[MCP client setup and tool reference](README.md)
