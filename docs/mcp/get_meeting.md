# `get_meeting`

A meeting's notes by its id (as results give it): title, start and end, participants as the notes name them and as canonical persons, summary, action items, and the transcript with include_transcript (segments with speaker, offsets and anchor seg-<id>).

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `id` | yes | The record's id, 64 lowercase hex characters, as results give it. |
| `include_transcript` | no | Return the transcript, segment by segment; it can be long. Default: only how many segments it has. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "get_meeting",
  "arguments": {
    "id": "<the meeting's id, as results give it>",
    "include_transcript": true
  }
}
```

[MCP client setup and tool reference](README.md)
