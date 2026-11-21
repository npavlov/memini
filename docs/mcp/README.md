# Connect your agent to Memini

Choose your client, then run the connection check below. Each connects to the same Memini server over Streamable HTTP. You can search and read sources with a workspace-scoped read token.

| Client | Setup |
|---|---|
| Claude Code | [Project config](#claude-code) |
| Codex CLI | [Register the server](#codex-cli) |
| OpenCode | [OpenCode setup](clients-terminal.md#opencode) |
| Pi coding agent | [Pi setup](clients-terminal.md#pi) |
| Cursor | [Cursor settings](clients-editors.md#cursor) |
| VS Code with GitHub Copilot | [Copilot settings](clients-editors.md#vs-code-with-github-copilot) |
| Gemini CLI | [Gemini settings](clients-editors.md#gemini-cli) |
| Windsurf | [Windsurf setup](clients-more.md#windsurf) |
| Zed | [Zed settings](clients-more.md#zed) |
| Cline | [Cline MCP settings](clients-more.md#cline) |
| Other MCP clients | [Connection settings](#other-clients) |

## Before you start

Start your [Memini server](../install.md), or ask its operator for the MCP URL and a read token for your workspace. For a local server the URL is `http://127.0.0.1:6364/mcp`. A client running in a container, SSH session or cloud needs an address reachable from that environment. Use HTTPS for a remote server.

If you administer the server and already have a workspace called `demo`, issue a read token once. These examples use the admin connection from the [installation guide](../install.md#5-the-first-admin-token). Substitute your workspace name if different.

**Bash or Zsh**

```sh
mkdir -p ~/.config/memini
( set -C; umask 077; memini-server cli token create --workspace demo --scope read --name mcp > ~/.config/memini/mcp.token )
```

**PowerShell** (with the `Save-MeminiToken` helper from the installation guide)

```powershell
Save-MeminiToken (Join-Path $HOME '.config/memini/mcp.token') {
    memini-server cli token create --workspace demo --scope read --name mcp
}
```

If an operator supplied a token file, use its path below. Open a fresh shell and load the read token there before launching your client. Use the token setting shown for your client. Keep token values out of shared project files.

```sh
export MEMINI_READ_TOKEN="$(cat ~/.config/memini/mcp.token)"
```

```powershell
$env:MEMINI_READ_TOKEN = (Get-Content -Raw (Join-Path $HOME '.config/memini/mcp.token')).Trim()
```

## Claude Code

For a personal setup in this project, run this command in Bash or Zsh, then run `claude mcp list`. Keep the single quotes so Claude reads the token from its environment at launch.

```sh
claude mcp add-json --scope local memini '{"type":"http","url":"http://127.0.0.1:6364/mcp","headers":{"Authorization":"Bearer ${MEMINI_READ_TOKEN}"}}'
```

For project-wide setup instead, add this entry to `.mcp.json`. Merge it with any existing `mcpServers` entries. Keep the literal `${MEMINI_READ_TOKEN}` in the file. [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson)

```json
{
  "mcpServers": {
    "memini": {
      "type": "http",
      "url": "http://127.0.0.1:6364/mcp",
      "headers": {"Authorization": "Bearer ${MEMINI_READ_TOKEN}"}
    }
  }
}
```

Start `claude` in that project from the prepared shell. Approve the project server when prompted. Open `/mcp` and look for **memini**.

## Codex CLI

Run once to register the URL and the name of the environment variable:

```sh
codex mcp add memini --url http://127.0.0.1:6364/mcp --bearer-token-env-var MEMINI_READ_TOKEN
codex mcp get memini
```

Check that the transport is Streamable HTTP and the bearer-token variable is `MEMINI_READ_TOKEN`. Start `codex` from the prepared shell and open `/mcp`. [Codex MCP setup](https://developers.openai.com/codex/mcp/)

## Other clients

Add an MCP server named `memini`, choose **Streamable HTTP**, set the MCP URL, and supply an `Authorization` header containing `Bearer ` followed by the read token. Use the client’s secret input or environment-variable mechanism. Each client has its own config syntax.

## Check the connection

Ask the agent: **“Call the Memini status tool and show which workspaces I can read.”** A successful response comes from the tool and includes the database status and visible workspaces. Then ask: **“Search my workspace for archive retention and show the source.”** For a searchable sample, follow [Your first search](../first-search.md).

| Symptom | What to check |
|---|---|
| Connection refused | Run `memini-server status` on the server machine. Check the hostname, port and `/mcp` path from the client. |
| HTTP 401 | Reload the token variable in the shell that starts the client, restart the client, and check whether the token was revoked. |
| HTTP 403 or missing workspace | Ask the operator to check the token’s scopes and workspace. |

## Tool reference

The examples use synthetic records from the harbour fixtures. Substitute names and IDs from
your workspace. Write tools also require the `ingest` scope. The MCP endpoint requires
`read` for the connection. `tools/list` provides the argument and response schemas for the
tools your token can call.

| Tool | Operation | Tool scope |
|---|---|---|
| [`search`](search.md) | Search | `read` |
| [`search_code`](search_code.md) | Search code | `read` |
| [`read_source`](read_source.md) | Read source | `read` |
| [`get_symbol`](get_symbol.md) | Get symbol | `read` |
| [`list_meetings`](list_meetings.md) | List meetings | `read` |
| [`get_meeting`](get_meeting.md) | Get a meeting | `read` |
| [`list_messages`](list_messages.md) | List messages | `read` |
| [`get_thread`](get_thread.md) | Get a thread | `read` |
| [`list_decisions`](list_decisions.md) | List decisions | `read` |
| [`get_decision`](get_decision.md) | Get a decision | `read` |
| [`status`](status.md) | Status | `read` |
| [`add_document`](add_document.md) | Add a document | `ingest` |
| [`record_decision`](record_decision.md) | Record a decision | `ingest` |
| [`supersede_decision`](supersede_decision.md) | Supersede decisions | `ingest` |
| [`close_decision`](close_decision.md) | Close a decision | `ingest` |
| [`retract_decision`](retract_decision.md) | Retract a decision | `ingest` |
