# Memini

[![CI](https://github.com/npavlov/memini/actions/workflows/ci.yml/badge.svg)](https://github.com/npavlov/memini/actions/workflows/ci.yml)
[![Security](https://github.com/npavlov/memini/actions/workflows/security.yml/badge.svg)](https://github.com/npavlov/memini/actions/workflows/security.yml)
[![Latest GitHub release including prereleases](https://img.shields.io/github/v/release/npavlov/memini?include_prereleases&sort=date&label=release)](https://github.com/npavlov/memini/releases)

Memini is a self-hosted search and decision journal for code repositories, work conversations and meeting notes. Search results link to the source file, message or transcript that supports them.

## Install

**Run your own server.** Start Docker with Compose 2.24+ and install the server launcher for your platform.

**macOS or Linux (Homebrew)**

```sh
brew tap npavlov/memini https://github.com/npavlov/memini.git
brew install npavlov/memini/memini-server
memini-server start
```

**Windows (PowerShell and Scoop)**

```powershell
scoop bucket add memini https://github.com/npavlov/memini.git
scoop install memini/memini-server
memini-server start
```

The first start creates private settings and downloads the application, database and search model. Allow 4 CPU cores, 8 GB RAM and 20 GB of free SSD space. [Get your first token](docs/install.md#5-the-first-admin-token) · [Illustrated installation guide](https://npavlov.github.io/memini/install.html) · [Manual Docker setup](docs/install.md#install-a-published-image-with-docker-only)

**Connect to an existing server.** Install the CLI with the [Windows x64 installer](docs/install-cli.md#windows-x64) or [Homebrew on Apple Silicon macOS and x86_64 Linux](docs/install-cli.md#homebrew-on-macos-and-linux). You’ll need the server address and a read token from its operator.

**Develop Memini.** [Build from source](docs/build-from-source.md).

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

- Locate an implementation by symbol and open its file at the indexed commit. [Code search](docs/mcp/search_code.md) · [Get a file](docs/mcp/get_file.md)
- Find who agreed to a change, read the evidence, and follow later decisions that replaced it. [Decision history](docs/mcp/get_decision.md)
- Find messages addressed to a person or meetings they attended. [Messages](docs/mcp/list_messages.md) · [Meetings](docs/mcp/list_meetings.md)
- Map what code belongs to: projects, domains and the services behind them, and how projects use each other. Search inside one domain across repositories. [Catalog](docs/mcp/get_catalog.md) · [Instruction for an agent](docs/mcp/catalog_guide.md)
- See who changed a file, a service or a domain. [Commit history](docs/mcp/list_commits.md)
- Run and connect Memini from a desktop app on macOS, Windows or Linux. [Desktop app](desktop/README.md)

Browse the **[online user knowledge base](https://npavlov.github.io/memini/)** or its [version in this checkout](docs/index.md) for setup, Intake, CLI and MCP tools, security, backup, upgrades and troubleshooting. For development and project history, see [Contributing](CONTRIBUTING.md), the [changelog](CHANGELOG.md) and the [Apache-2.0 license](LICENSE). [Report a vulnerability privately](SECURITY.md).
