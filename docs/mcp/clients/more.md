# Connect Windsurf, Zed, or Cline

Use a workspace token with `read` scope. For Windsurf or Cline, load `MEMINI_READ_TOKEN` and launch the editor from that shell so it inherits the variable. Merge the `memini` entry into your existing configuration. The examples assume the client and Memini run on the same machine. For a remote server, substitute its HTTPS MCP URL. [Create and load a read token](../README.md#before-you-start)

## Windsurf

For **Windsurf Cascade** on the stable channel, edit `~/.codeium/windsurf/mcp_config.json`. Cascade reads the bearer token from the environment when it loads the configuration. [Windsurf configuration locations](https://docs.devin.ai/cli/reference/configuration/read-config-from#windsurf) · [Microsoft APM’s Windsurf variable reference](https://microsoft.github.io/apm/reference/mcp-variable-compatibility/)

```json
{
  "mcpServers": {
    "memini": {
      "serverUrl": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${env:MEMINI_READ_TOKEN}" }
    }
  }
}
```

Refresh Cascade’s MCP server list, then ask Cascade to call Memini’s `status` tool. If your editor shows **Devin Local** instead, use `url` in place of `serverUrl` in the example. Save it in `~/.config/devin/mcp_config.json` (Windows: `%APPDATA%\devin\mcp_config.json`), then refresh **Customize → MCPs → This computer**. [Devin Local setup](https://docs.devin.ai/desktop/devin-local/customizations#mcp-servers) · [Devin MCP configuration](https://docs.devin.ai/cli/extensibility/mcp/configuration)

## Zed

Run `zed: open settings file` from the Command Palette to open your personal `settings.json`. Zed’s documented HTTP configuration takes a literal header value. Replace `<workspace-read-token>` with your token in that private user file and keep it out of project settings and chat. [Zed MCP configuration and connection checks](https://zed.dev/docs/ai/mcp)

```json
{
  "context_servers": {
    "memini": {
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer <workspace-read-token>" }
    }
  }
}
```

Open **Settings → AI → MCP Servers**. The green indicator beside **memini** should say **Server is active**. In the Zed Agent panel, ask it to call Memini’s `status` tool.

## Cline

In the Cline IDE extension, open **MCP Servers → Configure → Configure MCP Servers**. This opens the extension’s `cline_mcp_settings.json`. Set `type` to `streamableHttp` and keep the environment reference in the header. [Cline MCP configuration](https://docs.cline.bot/mcp/mcp-overview) · [Cline extension’s environment expansion](https://github.com/cline/cline/blob/main/apps/vscode/src/services/mcp/McpHub.ts)

```json
{
  "mcpServers": {
    "memini": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${env:MEMINI_READ_TOKEN}" }
    }
  }
}
```

In **MCP Servers**, expand **memini** and look for `status`. Ask Cline to call that tool and approve the request when prompted. [Memini status tool](../status.md)
