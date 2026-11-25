# `get_meeting`

A meeting's notes by record_id, or by source and external_id (as results give them): title, start and end, participants as the notes name them and as canonical persons, summary, action items, and the transcript with include_transcript (segments with speaker, offsets and anchor seg-<id>).

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `record_id` | no | The record's id, 64 lowercase hex characters, as results give it. |
| `source` | no | The record's source by name, with external_id. |
| `external_id` | no | The record's id in its source, as results give it, with source. |
| `workspace` | no | With source and external_id: the workspace; may be left out when only one workspace the token may read has such a record. |
| `include_transcript` | no | Return the transcript, segment by segment; it can be long. Default: only how many segments it has. |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the `sensitive` scope; without it the call is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_meeting",
  "arguments": {
    "source": "meetings",
    "external_id": "notes-1111-pilot-fees",
    "include_transcript": true
  }
}
```

[MCP client setup and tool reference](README.md)
