# `get_thread`

A whole thread by its id (as results give it): its title and channel, the canonical persons in it with their roles, and every message in order with its id, anchor msg-<id>, link, author, time, recipients, cc, mentions and text.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `id` | yes | The record's id, 64 lowercase hex characters, as results give it. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_thread",
  "arguments": {
    "id": "<the thread's id, as results give it>"
  }
}
```

[MCP client setup and tool reference](README.md)
