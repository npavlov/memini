# `list_decisions`

Read active decisions and open questions, newest first unless a query ranks them. Set history to include earlier states. Filters: workspaces, query (statement words), persons (participants: 'me', a person id or an alias), since and until (on occurred_at, when it was said), and projects and domains of the catalog (a domain includes its subdomains). Each item carries its id, occurred_at, created_at and context: its project and domains and how each came (push, default, manual, code, model). With projects or domains, without_domain counts the project's items that no domain holds. left_out counts the items whose evidence is all marked sensitive.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |
| `workspaces` | no | Workspaces |
| `persons` | no | Only items with one of these persons as a participant: 'me', a person id, or an alias as the sources show it (a mail address, a chat user id, a display name); a person stands for everyone merged into it. |
| `projects` | no | Only items under one of these projects of the catalog: their project, or a domain of theirs, is one of them. |
| `domains` | no | Only items in one of these domains: domain or domain/subdomain; a domain includes its subdomains. With projects, the domains of those projects. |
| `query` | no | Query |
| `since` | no | Since |
| `until` | no | Until |
| `history` | no | Include superseded, closed and retracted items. |
| `limit` | no | Limit |

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
