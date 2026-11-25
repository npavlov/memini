# `list_messages`

Messages of chat and mail threads, one result per message: by default those sent to a person (to or cc) or that mention them, 'which messages were sent to me about X'; roles picks recipient, mention or author (['author']: messages from the person). person is 'me' (the default: the person marked as the caller), a person id, or an alias as the sources show it (a mail address, a chat user id, a display name); a person stands for everyone merged into it. query keeps those whose text holds a word of it (stemmed English and Russian words, names and identifiers as written), best match first; without it, newest first. No language model is involved: for a search by meaning use search with the persons filter. Filters: since and until on the time the message was sent (ISO 8601 with a time zone), workspaces, sources. Each result carries its workspace, source and source kind, record_id, external_id, locator (source:external_id#anchor), link and time; record_id with anchor is an evidence reference for record_decision. A message carries its thread's title and channel, author, recipients, mentions and text, and the roles the person has in it. Read the whole thread with get_thread.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `person` | no | Whose: 'me' (the default: the person marked as the caller in each workspace), a person id, or an alias as the sources show it: a mail address, a chat user id or a display name. A person stands for everyone merged into it. |
| `roles` | no | What the person is in the message: recipient (to or cc), mention, author. Default: recipient and mention, the messages sent to the person or that mention them. Messages from a person: ['author']. |
| `query` | no | Words of the topic, in any language: only messages whose text holds a word of it come back, the best match first (words are stemmed in English and Russian; names and identifiers match as written). No language model is involved. Without it: newest first. For a search by meaning, use search with the persons filter. |
| `since` | no | Only messages sent at or after this time (ISO 8601 with a zone). |
| `until` | no | Only messages sent before this time (ISO 8601). |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `sources` | no | Only these sources, by name. |
| `limit` | no | The most results, 1 to 100. |
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the `sensitive` scope; without it the call is refused. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "list_messages",
  "arguments": {
    "person": "U0GUEST77",
    "roles": [
      "author"
    ]
  }
}
```

[MCP client setup and tool reference](README.md)
