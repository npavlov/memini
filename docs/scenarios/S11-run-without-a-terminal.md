# S11. Run Memini without a terminal

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator | ready | memini-qpk.5 |

## Who and pain

The operator starts, stops and connects Memini from a terminal only.

## Question

"Start the server and give me what to paste into my agent."

## Answer

The desktop app: start, stop and status of a local server, the MCP address with a ready client
configuration, tokens, a switch for the MCP endpoint, and a connection to a remote server by address
and token.

The first setup is one button, the search model included. On a Mac the model runs in Ollama on the
Mac, about a hundred times faster than in Docker, which has no GPU there: the step "Prepare the
search model" finds Ollama, downloads the model with its progress shown, and the server is installed
in that mode from the start. Without Ollama the step asks once: install it with Homebrew (the app
runs `brew install ollama` and `brew services start ollama`, and shows what Homebrew said), or go on
with the model in Docker, which the Server screen can move to the Mac later. Linux and Windows keep
the model in its container.

## Data

None. The app drives the existing launcher and the HTTP API. The MCP switch is a new setting of the
service.

## Check

One build per platform installs, starts a local server and connects to it. On a Mac with Ollama,
the setup needs no click besides Set up, and the first sync embeds a chunk in about a tenth of a
second, not twelve.

## Notes

To confirm: when the MCP endpoint is switched off while the server runs.

## See also

- [All scenarios](README.md)
- [S12](S12-see-and-edit-the-project-map.md): See the project map and correct it
- Tool reference: [status](../mcp/status.md)
