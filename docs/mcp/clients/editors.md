# Connect Cursor, VS Code with GitHub Copilot, or Gemini CLI

These clients can connect directly to Memini over Streamable HTTP. Start Memini, then obtain a token with `read` scope for the workspace you want the assistant to search. [MCP setup and token creation](../README.md)

## Prepare the client environment

Load the workspace token into the environment that launches the client. The examples assume the token is saved in `~/.config/memini/mcp.token`. Substitute your token file.

**Bash or Zsh**

```sh
export MEMINI_READ_TOKEN="$(cat "$HOME/.config/memini/mcp.token")"
```

**PowerShell**

```powershell
$env:MEMINI_READ_TOKEN = (Get-Content -Raw (Join-Path $HOME '.config/memini/mcp.token')).Trim()
```

Quit any running editor, then launch `cursor .`, `code .`, or `gemini` from that shell so the client inherits the variable. Merge the `memini` entry below into your existing settings to preserve other servers.

The examples use Memini's default local URL. In a remote workspace or dev container, `127.0.0.1` refers to the environment making the connection. Supply the token there and use your operator's HTTPS endpoint or a local tunnel to Memini. To check network reachability without sending a token, open `http://127.0.0.1:6364/readyz` from the same environment, adjusting the address as needed. A ready server returns HTTP 200.

## Cursor

Add this entry to your personal `~/.cursor/mcp.json` or the project's `.cursor/mcp.json`. Cursor resolves `${env:MEMINI_READ_TOKEN}` in HTTP headers. [Cursor MCP configuration](https://cursor.com/docs/mcp)

```json
{
  "mcpServers": {
    "memini": {
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${env:MEMINI_READ_TOKEN}" }
    }
  }
}
```

Open Cursor's MCP settings, enable `memini`, then use Agent chat to run the check below.

## VS Code with GitHub Copilot

For a session using the **Copilot harness**, edit `~/.copilot/mcp-config.json` (or `$COPILOT_HOME/mcp-config.json` when set). The workspace equivalent is `.mcp.json` at the project root. This format supports `${MEMINI_READ_TOKEN}` inside the header value. The older Local harness uses a separate editor-managed `.vscode/mcp.json` format. [VS Code MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

```json
{
  "mcpServers": {
    "memini": {
      "type": "http",
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${MEMINI_READ_TOKEN}" },
      "tools": ["*"]
    }
  }
}
```

In Chat, select the Copilot harness. Run **Chat: Open Customizations** from the Command Palette, select **MCP Servers** and start or enable `memini`. Use **Show Output** in the server menu to inspect connection failures. [VS Code server controls](https://code.visualstudio.com/docs/agents/run/manage-mcp-servers)

## Gemini CLI

Add this entry to your personal `~/.gemini/settings.json` or the project's `.gemini/settings.json`. Gemini CLI selects Streamable HTTP with `httpUrl` and expands environment references when it loads the settings. [Gemini CLI MCP transports](https://geminicli.com/docs/tools/mcp-server/) · [Gemini CLI settings](https://geminicli.com/docs/reference/configuration/)

```json
{
  "mcpServers": {
    "memini": {
      "httpUrl": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${MEMINI_READ_TOKEN}" }
    }
  }
}
```

Run `gemini mcp list` from the configured shell and look for `memini` marked connected. Start `gemini` and run the check below.

## Verify workspace access

Ask the assistant **“Call the Memini status tool with no arguments. Show the workspace names from its result.”** Check the tool call and confirm that your workspace appears. An empty workspace can have zero records. If the connection returns HTTP 401, check the client process received the token value and that the token is current. If your workspace is missing, check which workspace the token grants access to. [Memini status tool](../status.md)
