# `list_meetings`

Meetings persons took part in (listed as a participant, or a speaker of the transcript): 'which meetings did I attend about X'. persons are 'me' (the default: the person marked as the caller), person ids, or aliases as the sources show them (a mail address, a chat user id, a display name); a person stands for everyone merged into it, and several persons count together. query keeps those whose text holds a word of it (stemmed English and Russian words, names and identifiers as written), best match first; without it, newest first. No language model is involved: for a search by meaning use search with the persons filter. Filters: since and until on the meeting's start (ISO 8601 with a time zone), workspaces, sources, projects, domains. projects and domains (a domain includes its subdomains; one is domain/subdomain) keep those linked to the catalog: pushed with it, by a default of their source or stream, or else by the domains the model gave the decisions taken from them; get_catalog lists the names. Each result carries its workspace, source and source kind, the record's id, external_id, locator (source:external_id#anchor), link and time; the record's id with anchor is an evidence reference for record_decision. left_out counts the results of records marked sensitive that were left out of items. With a query, a meeting also carries the passage that matched (anchor, snippet). Read a meeting's notes with get_meeting.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |
| `persons` | no | Whose: 'me' (the default: the person marked as the caller in each workspace), person ids, or aliases as the sources show them: a mail address, a chat user id or a display name. A person stands for everyone merged into it; several persons count together. |
| `query` | no | Words of the topic, in any language: only meetings whose text holds a word of it come back, the best match first (words are stemmed in English and Russian; names and identifiers match as written). No language model is involved. Without it: newest first. For a search by meaning, use search with the persons filter. Their text is the title, summary, action items and transcript. |
| `since` | no | Only meetings that started at or after this time (ISO 8601 with a zone). |
| `until` | no | Only meetings that started before this time (ISO 8601). |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `sources` | no | Only these sources, by name. |
| `projects` | no | Only meetings linked to these projects of the catalog: pushed with one, by a default of their source or stream, or else by the domains the model gave the decisions taken from them. A name the catalog does not hold is an error; get_catalog lists the names. |
| `domains` | no | Only meetings linked to these domains of the catalog, as projects are: 'domain', which includes its subdomains, or 'domain/subdomain'. |
| `limit` | no | The most results, 1 to 100. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "list_meetings",
  "arguments": {
    "persons": ["Mira Castellane"]
  }
}
```

[MCP client setup and tool reference](README.md)
