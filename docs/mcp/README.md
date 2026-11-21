# MCP client setup and tool reference

Memini serves Streamable HTTP at `http://127.0.0.1:6364/mcp`. Create a workspace-scoped
read token using an admin connection:

```sh
mkdir -p ~/.config/memini
(umask 077; bin/memini token create --workspace harbor --scope read --name mcp > ~/.config/memini/harbor.token)
```

For **Codex**, register the endpoint and launch the client with the token in its environment:

```sh
codex mcp add memini --url http://127.0.0.1:6364/mcp --bearer-token-env-var MEMINI_READ_TOKEN
export MEMINI_READ_TOKEN="$(cat ~/.config/memini/harbor.token)"
codex
```

For **Claude Code**, use the [tested quickstart](../install.md#8-connect-claude-code) with the
workspace token above. For another HTTP client, configure the same URL and an
`Authorization: Bearer <token>` header. Connect and call `status` to check workspace access.

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
