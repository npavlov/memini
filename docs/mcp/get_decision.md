# `get_decision`

Read an item and its supersession chain with evidence links and raw record states.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `item_id` | yes | Item Id |

## Example

Send these parameters to `tools/call`.
Replace the bracketed IDs with IDs from `record_decision` or `list_decisions`.

```json
{
  "name": "get_decision",
  "arguments": {
    "item_id": "<item_id>"
  }
}
```

[MCP client setup and tool reference](README.md)
