# `get_thread`

A whole thread by record_id, or by source and external_id (as results give them): its title and channel, the canonical persons in it with their roles, and every message in order with its id, anchor msg-<id>, link, author, time, recipients, cc, mentions and text.

Token scope: `read`.

| Argument | Required | Description |
|---|---|---|
| `record_id` | no | The record's id, 64 lowercase hex characters, as results give it. |
| `source` | no | The record's source by name, with external_id. |
| `external_id` | no | The record's id in its source, as results give it, with source. |
| `workspace` | no | With source and external_id: the workspace; may be left out when only one workspace the token may read has such a record. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_thread",
  "arguments": {
    "source": "chat",
    "external_id": "C0GENERAL1/1793955600.000100"
  }
}
```

[MCP client setup and tool reference](README.md)
