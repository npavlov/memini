# `status`

Whether Memini's knowledge base is current. Per stream of every workspace the token may read: its source, schedule, lag (seconds since everything up to checked_until was stored), why it is behind if it is (lag, never checked, records not indexed, a stale index), its last run, failed runs in a row with the first line of the last error, how far back its history is loaded, its git pin, and whether what it follows is missing at the source (a deleted branch). Per workspace: its index (ok, stale, none or off), record and chunk counts, and the command that brings it up to date. Also the database and schema version. Read-only; takes no arguments.

Token scope: `read`.

## Example

Send these parameters to `tools/call`.

```json
{
  "name": "status",
  "arguments": {}
}
```

[MCP client setup and tool reference](README.md)
