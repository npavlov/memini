# `search`

Search everything the token may read: code, message threads, meetings and documents. Ranks chunks by meaning and by words (mode hybrid, the default; dense: meaning only; lexical: words only). Filters: workspaces, sources, kinds (code_file, thread, meeting, document, decision, open_question), since and until (ISO 8601), persons (records that name them: 'me', a person id, or an alias such as a mail address, a chat user id or a display name; merged persons count as one) and roles (with persons: author, recipient, mention, participant). Active items matching statement words or citing retrieved records are in decisions. Use get_decision for their full history. projects and domains (a domain includes its subdomains; one is domain/subdomain) narrow to the code the catalog names, by its path masks as they are now, and to the threads, meetings and documents linked to them: pushed with them, by a default of their source or stream, or else by the domains the model gave the decisions taken from them. services narrows to code. A name the catalog does not hold is an error, get_catalog lists them. A hit of a file of a repository carries context: the project and service it is in and the domains that name it. With projects or domains, decisions holds only items under those projects and in those domains, as list_decisions reads them; with services, only items citing retrieved records. Each hit carries its id (the chunk's), source kind, workspace and source, locator (repo@sha:path#Lstart-Lend for code; source:external_id#anchor otherwise), occurred_at (the record's time, which since and until filter on), and the chunk's text as snippet. left_out counts the hits of records marked sensitive that were left out of items. A hit of a thread, a meeting or a document also carries anchor, its place in the record (msg-<id>, seg-<id>, summary/<heading trail>, action-items, a heading trail), link (the message's URL, else the record's) and chunk_occurred_at (when the chunk's content happened: its first message, its transcript window). record_id with anchor is an evidence reference: to cite the hit in record_decision, pass both as they are. For exact, regex or symbol search in code use search_code. For the meetings or the messages of a person, one result per meeting or message, use list_meetings and list_messages.

Token scope: `read`; `sensitive` too for `include_sensitive`.

| Argument | Required | Description |
|---|---|---|
| `include_sensitive` | no | Also read records marked sensitive (a salary, a diagnosis), which are left out by default. Needs a token with the sensitive scope; without it the request is refused. |
| `query` | yes | A question or keywords, in any language. |
| `limit` | no | The most results, 1 to 100. |
| `workspaces` | no | Only these workspaces, by name; default: every workspace the token may read. |
| `sources` | no | Only these sources, by name. |
| `kinds` | no | Filter record and journal results by kind. |
| `since` | no | Only records that occurred at or after this time (ISO 8601 with a zone). |
| `until` | no | Only records that occurred before this time (ISO 8601). |
| `persons` | no | Only records that name one of these persons: 'me' (the person marked as the caller), a person id, or an alias as the sources show it (a mail address, a chat user id, a display name). A person stands for everyone merged into it. A name that matches no one gives no hits. |
| `roles` | no | With persons: only records where the person is one of these: author (wrote a message), recipient (a message was sent to them, to or cc), mention (a message or an action item names them), participant (took part in a meeting). Default: any. |
| `projects` | no | Only results of these projects of the catalog, by name: the code of their services and domains, and the records linked to them. Threads, meetings and pushed documents count when they are linked to it: pushed with it, by a default of their source or stream, or else by the domains the model gave the decisions taken from them. With domains or services, the projects those names are looked up in. A name the catalog does not hold is an error; get_catalog lists the names. |
| `domains` | no | Only results of these domains of the catalog: 'domain', which includes its subdomains, or 'domain/subdomain'. Code by the catalog's path masks as they are now. Threads, meetings and pushed documents count when they are linked to it: pushed with it, by a default of their source or stream, or else by the domains the model gave the decisions taken from them. |
| `services` | no | Only code of these services of the catalog, by name: a repository or a directory. |
| `mode` | no | hybrid: by meaning and by words, fused (the default); dense: by meaning only; lexical: by words only. |

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "search",
  "arguments": {
    "query": "harbour fees",
    "limit": 5
  }
}
```

[MCP client setup and tool reference](README.md)
