# Memini

[![CI](https://github.com/npavlov/memini/actions/workflows/ci.yml/badge.svg)](https://github.com/npavlov/memini/actions/workflows/ci.yml)
[![Security](https://github.com/npavlov/memini/actions/workflows/security.yml/badge.svg)](https://github.com/npavlov/memini/actions/workflows/security.yml)
[![Latest GitHub release including prereleases](https://img.shields.io/github/v/release/npavlov/memini?include_prereleases&sort=date&label=release)](https://github.com/npavlov/memini/releases)

Memini is a self-hosted search and decision journal for code repositories, work conversations and meeting notes. Search results link to the source file, message or transcript that supports them.

## Install

Start with **4 CPU cores, 8 GB RAM and 20 GB of free SSD** on x86-64 or arm64. You need Docker with Compose 2.24 or later. Ollama runs the embedding model locally. A GPU is optional and speeds up the first index.

- **Build from source** with Git, Docker and Ollama. Follow the [installation guide](docs/install.md#what-you-need) through step 5 to start the service and save the first admin token.
- **Use a published image** when a matching release tag and GHCR image are available. This [Docker-only installation](docs/install.md#install-a-published-image-with-docker-only) runs Ollama inside Compose too.
- **Connect to an existing server** with just the CLI. After the RC is published to PyPI, use Python 3.12+ and `pipx install memini==0.1.0rc1`. Set `MEMINI_URL` to the server address and `MEMINI_TOKEN_FILE` to the token file supplied by its operator. Check access with `memini workspace list`.

From the checkout, `docker compose ps` should show healthy services, and `bin/memini workspace list` should list the workspaces your token can read. For a failed health check or an unreachable model, see [installation troubleshooting](docs/install.md#troubleshooting).

## First steps

Create a workspace, import one example email conversation and find who proposed changing archive retention. The [first search walkthrough](docs/first-search.md) includes the sample data, commands and expected results. To start with code, [index one file from a Git repository](docs/install.md#6-a-workspace-and-a-repository) and search for its bootstrap function.

## Intake

| What you have | How to bring it in |
|---|---|
| A Git repository | [Register the repository and schedule a sync](docs/install.md#6-a-workspace-and-a-repository). Memini tracks the selected ref. |
| Mail, chat, notes or wiki exports | [Push records from a file or the HTTP API](docs/intake.md#without-an-agent). Use one source per mailbox to keep accounts separate. |
| A mailbox or chat available inside an agent session | [Give the agent a bounded import](docs/intake.md#with-an-agent). It can push complete threads or capture a recipe's MCP responses. |
| An MCP server reachable from the service | [Set up a scheduled MCP source](docs/intake.md#scheduled-mcp-intake). |

## Use Memini

- Locate an implementation by symbol and open its file at the indexed commit. [Code search](docs/mcp/search_code.md) · [Read source](docs/mcp/read_source.md)
- Find who agreed to a change, read the evidence, and follow later decisions that replaced it. [Decision history](docs/mcp/get_decision.md)
- Find messages addressed to a person or meetings they attended. [Messages](docs/mcp/list_messages.md) · [Meetings](docs/mcp/list_meetings.md)

Browse the **[online user knowledge base](https://npavlov.github.io/memini/)** or its [version in this checkout](docs/index.md) for setup, Intake, CLI and MCP tools, security, backup, upgrades and troubleshooting. For development and project history, see [Contributing](CONTRIBUTING.md), the [changelog](CHANGELOG.md) and the [Apache-2.0 license](LICENSE). [Report a vulnerability privately](SECURITY.md).
