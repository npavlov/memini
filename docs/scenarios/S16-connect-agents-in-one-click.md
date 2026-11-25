# S16. Connect my agent clients in one click

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator, engineer | planned | memini-qpk.11 |

## Who and pain

The person who runs Memini copies its MCP configuration by hand into each agent client: a file to
find, a token to create, a variable to set, per client and again after every change.

## Question

"Connect Claude Code and Cursor to this server."

## Answer

The MCP screen of the desktop app lists the agent clients found on this computer, among those the
[MCP guide](../mcp/README.md) documents: Claude Code, Cursor, Codex CLI, VS Code with Copilot,
Gemini CLI, Windsurf, Zed, Cline and OpenCode. A client that is not installed is not offered.

- **Connect** writes the server entry named `memini` into that client's own configuration, with a
  read token the app creates and names for that client.
- **Disconnect** removes that entry and revokes that token.

Each happens on a click for one client, never by itself. Only the entry named `memini` is touched:
the rest of the file is kept, and the file is backed up before the first change. The token is
passed the safest way the client offers, a token file or the client's own reference to an
environment variable; where a client takes only a literal token, the app says so before it writes
one. The state the screen shows is read from the client's file, not remembered. A configuration the
app cannot read as that client's format is refused and left as it is.

## Data

None of its own. It uses the token routes of the HTTP API (`POST /v1/tokens`,
`DELETE /v1/tokens/{id}`) and the configuration patterns of the MCP guide.

## Check

For each supported client, on a fixture copy of its configuration: Connect adds a valid entry and
keeps the rest of the file; Connect twice changes nothing; Disconnect removes only the entry and
revokes the token; a malformed configuration is refused without a change; the token is never
logged; each written entry is compared with the pattern in the MCP guide.

## See also

- [All scenarios](README.md)
- [S11](S11-run-without-a-terminal.md): Run Memini without a terminal
- [S15](S15-see-what-memini-holds.md): See what Memini holds and try its tools
- Client setup by hand: [Connect your agent to Memini](../mcp/README.md)
