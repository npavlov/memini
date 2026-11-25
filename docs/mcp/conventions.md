# API conventions

One way to name things, for every MCP tool, HTTP route and CLI command of Memini. There are no
aliases and no second spellings: a name that breaks a rule is a bug, and
[`tests/test_api_conventions.py`](../../tests/test_api_conventions.py) fails on it. The rules
hold from 0.2; the names 0.1 used are listed in the [changelog](../../CHANGELOG.md).

## Tools

A tool's name is a verb and a noun. Six verbs, each with one meaning:

| Verb | Returns or does | Tools |
|---|---|---|
| `get` | one object, by its `id` or, for the catalog and files, by its name or path | `get_file`, `get_thread`, `get_meeting`, `get_decision`, `get_catalog`, `get_project`, `get_domain` |
| `list` | objects that match filters, newest first | `list_messages`, `list_meetings`, `list_decisions`, `list_commits` |
| `search` | objects that match a query, best first | `search`, `search_code` |
| `put` | creates or replaces one object; sent again, it changes nothing | `put_document`, `put_project` |
| `record` | appends to a journal | `record_decision` |
| `supersede`, `close`, `retract` | move a journal item to its next state | `supersede_decision`, `close_decision`, `retract_decision` |

`status` and `catalog_guide` name what they return and take no arguments. A definition of a
symbol is found with `search_code` in mode `symbol` (whole names) or `symbol_prefix`.

## One object, many objects

- **One object** is named in the singular: `workspace`, `source`, `project`, `domain`, `path`,
  and `id` for an object that has an id (a record, a decision, a person, a token, a run). A
  record is named by its id only, never by its source and external id.
- **A filter** is a list named in the plural: `workspaces`, `sources`, `kinds`, `persons`,
  `roles`, `projects`, `domains`, `services`, `paths`, `languages`, `linked_projects`,
  `include_paths`, `exclude_paths`. A filter left out does not narrow.
- **An object's own id** is `id`. **A reference** to another object is that object's noun and
  `_id`: a search hit's `record_id`, a commit author's `person_id`, a link's `item_id`, a
  run's `run_id`.
- **Text to look for** is `query`, in every tool that takes it, whatever the mode reads it as: a
  question, words, a substring, a regular expression, a symbol name. `text` is a document's own
  text, never a filter.
- **People** are `persons`: `me`, a person id, or an alias (a mail address, a chat user id, a
  display name). What a person is in a record is `roles`. In `list_commits` a value that names no
  person is text the author's name or address holds.
- **Catalog filters** are `projects`, `domains` (a domain includes its subdomains; one is
  `domain/subdomain`) and, for code, `services`, on every tool that returns code or records.

## Sources

A source is where records come from. Its `kind` is `repository` (a git repository the service
syncs), `push` (records sent to the service) or `mcp` (an MCP server the service calls). Every
argument and field names a source as `source` or `sources`, whatever its kind: there is no
`repository` field. A source of kind `repository` is called a repository in prose.

## Time

- `occurred_at`: when the thing happened. A message was sent, a meeting started, a commit was
  made, a decision was taken. Every object that has such a time carries it under this name; the
  item a search hit is in carries it, and the hit's own passage carries `chunk_occurred_at`.
- `created_at`, `updated_at`: when Memini stored the object and last changed it.
- `since` and `until` filter on `occurred_at`: `since` inclusive, `until` exclusive, ISO 8601
  with a zone.
- Any other moment of an object's life is a past tense with `_at`: `started_at`, `ended_at`,
  `finished_at`, `expires_at`, `revoked_at`, `forgotten_at`, `marked_at`.

The payloads pushed to the service keep the field names of their formats (`sent_at` of a message
in a thread, [intake.md](../intake.md)): they are the records as their sources wrote them.

## Answers

- A list or a search answers `{"items": [...]}`.
- `left_out` counts what the service took out of `items` and the caller may ask for: the results
  of records marked sensitive, read with `include_sensitive` and a token with the `sensitive`
  scope.
- A list that can be continued answers `next_cursor`; the same call with it as `cursor`, and the
  other arguments unchanged, gives what follows. `null`: the list is whole.
- `limit` caps how many objects an answer holds.

## Errors

An MCP tool answers an error result with a message; an HTTP route the error shape of the API,
`{"error": {"code", "message", "request_id", "details"}}`. The code is a stable word: `not_found`
(404) for anything the token may not read or that does not exist, `sensitive_record` (403) for a
record marked sensitive read without `include_sensitive`, `insufficient_scope` (403),
`validation_error` (422, with the fields in `details`). A message never repeats a secret or stored
text.

## HTTP

- Paths are nouns in the plural, nested under the workspace when the object belongs to one:
  `/v1/workspaces/{workspace}/sources/{source}`, `/v1/workspaces/{workspace}/records/{id}/forget`.
- A path parameter is the singular noun of a named object (`{workspace}`, `{source}`,
  `{project}`, `{domain}`, `{path}`), `{id}` for the object with an id the path ends at, and
  `{person_id}` for the person an alias belongs to.
- Query parameters and body fields have the names of the tool's arguments; a list filter repeats
  its parameter (`?workspaces=a&workspaces=b`).
- `GET` reads, `PUT` creates or replaces (sent again, it changes nothing), `POST` appends or acts,
  `PATCH` changes some fields, `DELETE` removes.

## CLI

- A filter is an option in the singular that repeats and goes to the service as the plural list:
  `--workspace a --workspace b` is `workspaces`, `--person` is `persons`, `--source` is `sources`.
- The object a command acts on is a positional argument: its name, or `<id>` for an object with an
  id (`memini decision close <workspace> <id>`, `memini forget <workspace> <id>`).
- An option has the name of the argument it sends, with dashes: `--occurred-at`, `--include`
  and `--exclude` for the paths of `include_paths` and `exclude_paths`.
