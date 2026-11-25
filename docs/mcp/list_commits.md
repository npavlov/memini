# `list_commits`

Who changed what, and when: the commit history of the git repositories the token may read, newest first, read from the service's mirror when asked and never stored. Each repository is walked from its pinned commit, the one search results are at. Filters: repositories, workspaces, paths (globs with .gitignore rules, e.g. src/fees/** or src/fees/calc.py: commits that changed such a path, listed with those paths), author (text in the author's name or mail address), since and until (ISO 8601 with a time zone, on the committer time), text (words in the message, as written; there is no search by meaning over messages). Each commit carries its workspace and repository, sha and short_sha, a locator repo@sha, occurred_at (the committer time) and authored_at, the author as git shows it and the person the author resolves to (person_id), subject, body, parents, and the changed paths with their status. A list cut by a bound says so (truncated, cut_by) and gives next_until: pass it as until to continue. A walk that runs past the service's time limit is an error: narrow it with repositories, paths, since or until. Read a file as a commit left it with read_source and the sha as ref.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `repositories` | no | Only these repositories (git sources), by name; default: every repository the token may read. |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `paths` | no | Only commits that changed a path matching one of these globs, each listed with those paths only. The rules are those of .gitignore: * and ? within a directory, ** across directories (src/**/*.py); a glob without / matches at any depth (*.py); a directory matches everything below it (src/fees). A glob that starts with a directory or names a file (src/fees/calc.py) is answered fastest. |
| `author` | no | Only commits whose author's name or mail address, as git shows them, holds this text, ignoring case. |
| `since` | no | Only commits committed at or after this time (ISO 8601 with a zone). |
| `until` | no | Only commits committed before this time (ISO 8601 with a zone). To continue a list that was cut, pass its next_until. |
| `text` | no | Only commits whose message holds this text, ignoring case: the words as written, not a pattern and not a search by meaning. |
| `limit` | no | The most commits, 1 to 1000; the service returns no more than its own limit (100 unless its operator changed git_log_max_commits) and says when that cut the list. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "list_commits",
  "arguments": {
    "repositories": [
      "harbour-code"
    ],
    "paths": [
      "src/berth_hold.py"
    ]
  }
}
```

[MCP client setup and tool reference](README.md)
