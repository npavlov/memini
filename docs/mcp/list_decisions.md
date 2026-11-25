# `list_decisions`

Read active decisions and open questions, newest first unless a query ranks them. Set history to include earlier states. Filter by workspace, statement words, participant ids, decision time, and projects and domains of the catalog (a domain includes its subdomains). Each item carries its context: its project and domains and how each came (push, default, manual, code, model). With projects or domains, without_domain counts the project's items that no domain holds.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `workspaces` | no | Workspaces |
| `person_ids` | no | Person Ids |
| `projects` | no | Only items under one of these projects of the catalog: their project, or a domain of theirs, is one of them. |
| `domains` | no | Only items in one of these domains: domain or domain/subdomain; a domain includes its subdomains. With projects, the domains of those projects. |
| `query` | no | Query |
| `since` | no | Since |
| `until` | no | Until |
| `history` | no | Include superseded, closed and retracted items. |
| `limit` | no | Limit |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the `sensitive` scope; without it the call is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "list_decisions",
  "arguments": {
    "workspaces": [
      "harbor"
    ],
    "domains": [
      "berths"
    ],
    "limit": 5
  }
}
```

[MCP client setup and tool reference](README.md)
