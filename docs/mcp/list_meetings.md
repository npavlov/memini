# `list_meetings`

Meetings a person took part in (listed as a participant, or a speaker of the transcript): 'which meetings did I attend about X'. person is 'me' (the default: the person marked as the caller), a person id, or an alias as the sources show it (a mail address, a chat user id, a display name); a person stands for everyone merged into it. query keeps those whose text holds a word of it (stemmed English and Russian words, names and identifiers as written), best match first; without it, newest first. No language model is involved: for a search by meaning use search with the persons filter. Filters: since and until on the meeting's start (ISO 8601 with a time zone), workspaces, sources. Each result carries its workspace, source and source kind, record_id, external_id, locator (source:external_id#anchor), link and time; record_id with anchor is an evidence reference for record_decision. With a query, a meeting also carries the passage that matched (anchor, snippet). Read a meeting's notes with get_meeting.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `person` | no | Whose: 'me' (the default: the person marked as the caller in each workspace), a person id, or an alias as the sources show it: a mail address, a chat user id or a display name. A person stands for everyone merged into it. |
| `query` | no | Words of the topic, in any language: only meetings whose text holds a word of it come back, the best match first (words are stemmed in English and Russian; names and identifiers match as written). No language model is involved. Without it: newest first. For a search by meaning, use search with the persons filter. Their text is the title, summary, action items and transcript. |
| `since` | no | Only meetings that started at or after this time (ISO 8601 with a zone). |
| `until` | no | Only meetings that started before this time (ISO 8601). |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `sources` | no | Only these sources, by name. |
| `limit` | no | The most results, 1 to 100. |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the `sensitive` scope; without it the call is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "list_meetings",
  "arguments": {
    "person": "Mira Castellane"
  }
}
```

[MCP client setup and tool reference](README.md)
