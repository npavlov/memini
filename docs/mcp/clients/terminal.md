# Connect OpenCode or Pi

Set `MEMINI_READ_TOKEN` in the shell where you will launch the client. Use a read token scoped to the `demo` workspace. The shared setup includes Bash and PowerShell commands. [Prepare your token](../README.md#before-you-start)

## OpenCode

Run `opencode --version` to choose the config layout. In your project’s `opencode.jsonc`, merge the following entry with any MCP servers you already use. OpenCode expands `{env:MEMINI_READ_TOKEN}` when it loads the file.

**OpenCode 1.x** uses the server name directly under `mcp`. [OpenCode 1.x MCP configuration](https://github.com/anomalyco/opencode/blob/v1.19.0/packages/web/src/content/docs/mcp-servers.mdx)

```json
{
  "mcp": {
    "memini": {
      "type": "remote",
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer {env:MEMINI_READ_TOKEN}" },
      "oauth": false
    }
  }
}
```

**OpenCode 2.x** places the same server entry under `mcp.servers`. [OpenCode MCP configuration](https://opencode.ai/docs/mcp-servers/)

```json
{
  "mcp": {
    "servers": {
      "memini": {
        "type": "remote",
        "url": "http://127.0.0.1:6364/mcp",
        "headers": { "Authorization": "Bearer {env:MEMINI_READ_TOKEN}" },
        "oauth": false
      }
    }
  }
}
```

Run `opencode mcp list` from the project directory. Look for `memini` marked `connected`, then launch `opencode` from that shell.

## Pi

Pi has a built-in Streamable HTTP client. Add the entry to your personal `~/.pi/agent/mcp.json`, or use `.pi/mcp.json` in a trusted project. Pi expands `${MEMINI_READ_TOKEN}` in the header. [Pi MCP configuration](https://docs.pi.dev/latest/mcp)

```json
{
  "mcpServers": {
    "memini": {
      "url": "http://127.0.0.1:6364/mcp",
      "headers": { "Authorization": "Bearer ${MEMINI_READ_TOKEN}" }
    }
  }
}
```

Run `pi mcp list`. A successful connection lists `memini: connected` and tools including `status` and `search`. Start `pi` in that shell. Use `/mcp` inside Pi to inspect the server or reconnect it.

## Check workspace access

Ask the agent: “Call the Memini `status` tool and show me the index status for workspace `demo`.” If the connection returns HTTP 401, reload the read token in the client’s shell and restart the client. If the connection is refused, check that the Memini server is running and that its address is reachable from the client. [Memini connection check](../README.md#check-the-connection)
