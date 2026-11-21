# `list_decisions`

Read active decisions and open questions. Set history to include earlier states. Filter by workspace, statement words, participant ids and decision time.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `workspaces` | no | Workspaces |
| `person_ids` | no | Person Ids |
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
    ]
  }
}
```

[MCP client setup and tool reference](README.md)
