# `get_decision`

Read an item by its id, and its supersession chain with evidence links and raw record states.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |
| `id` | yes | Id |

## Example

Send these parameters to `tools/call`.
Replace the bracketed IDs with IDs from `record_decision` or `list_decisions`.

```json
{
  "name": "get_decision",
  "arguments": {
    "id": "<item id>"
  }
}
```

[MCP client setup and tool reference](README.md)
