# `get_file`

Read a file, a range of its lines, or a directory listing from a repository the token may read. By default at the pinned commit of the repository's default tracked branch, the commit search results are at; ref takes a branch or tag name, a full ref or a full commit id. path is relative to the repository root (empty for the root); absolute paths, '..' and control characters are refused. Lines are 1-based and inclusive, at most 2000 lines and 100000 characters per call: when truncated, continue from end_line + 1. A binary file or one over the size limit returns no content; a symlink returns its target, not followed. The result carries the commit, its time, a locator repo@sha:path#Lstart-Lend, the definitions in the lines read, and context: the project, service and domains the path belongs to by the catalog.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `repository` | yes | The repository (git source) by name. |
| `path` | no | A file or directory, relative to the repository root; empty for the root. |
| `workspace` | no | The repository's workspace; may be left out when only one workspace the token may read has a repository of that name. |
| `ref` | no | A branch or tag name (main, v1.0), a full ref (refs/heads/main), or a full commit id. Default: the repository's default tracked branch. A tracked ref is read at its pinned commit, the one search results are at; any other ref at the commit the mirror has for it. |
| `start_line` | no | First line to read, 1-based; default 1. |
| `end_line` | no | Last line to read, inclusive; default: as far as the limits allow. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_file",
  "arguments": {
    "repository": "harbour-code",
    "path": "src/berth_hold.py"
  }
}
```

[MCP client setup and tool reference](README.md)
