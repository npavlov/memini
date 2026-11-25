# S11. Run Memini without a terminal

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator | planned | memini-qpk.5 |

## Who and pain

The operator starts, stops and connects Memini from a terminal only.

## Question

"Start the server and give me what to paste into my agent."

## Answer

The desktop app: start, stop and status of a local server, the MCP address with a ready client
configuration, tokens, a switch for the MCP endpoint, and a connection to a remote server by address
and token.

## Data

None. The app drives the existing launcher and the HTTP API. The MCP switch is a new setting of the
service.

## Check

One build per platform installs, starts a local server and connects to it.

## Notes

To confirm: when the MCP endpoint is switched off while the server runs.

## See also

- [All scenarios](README.md)
- [S12](S12-see-and-edit-the-project-map.md): See the project map and correct it
- Tool reference: [status](../mcp/status.md)
