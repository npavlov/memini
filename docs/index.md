# Memini user knowledge base

Start with [Install](install.md), then [run your first search](first-search.md). Choose a guide below for the task you want to do.

| Task | Guide |
|---|---|
| Check machine requirements and install | [Requirements and prerequisites](install.md#what-you-need) · [Machine sizing](operations/performance.md#the-smallest-supported-machine) · [Homebrew and Scoop server setup](install.md#install-the-server-launcher) · [Remote CLI client](install-cli.md) · [Manual Docker installation](install.md#install-a-published-image-with-docker-only) |
| Import one or several mailboxes, chat, meetings or wiki pages | [Intake without an agent](intake.md#without-an-agent) · [Intake with an agent](intake.md#with-an-agent) · [Record fields and payload examples](intake.md#records) · [HTTP API example](intake.md#the-same-with-curl) |
| Index a Git repository or an MCP server | [Tracked Git refs and schedules](install.md#6-a-workspace-and-a-repository) · [Scheduled MCP intake](intake.md#scheduled-mcp-intake) · [Write and test a recipe](recipes.md#author-a-recipe-and-parser) |
| Search code or open a source file | [Search modes and filters](mcp/search_code.md) · [Find a symbol](mcp/get_symbol.md) · [Read the indexed commit](mcp/read_source.md) · [See who changed what](mcp/list_commits.md) |
| Say what code belongs to: projects, services and domains | [Instruction for an agent](mcp/catalog_guide.md) · [Write a project](mcp/put_project.md) · [Read the catalog](mcp/get_catalog.md) · [A project and its links](mcp/get_project.md) · [A domain and its code](mcp/get_domain.md) |
| Read conversations and meeting notes | [Messages](mcp/list_messages.md) · [Threads](mcp/get_thread.md) · [Meetings](mcp/list_meetings.md) · [Transcripts](mcp/get_meeting.md) |
| Trace a decision or record a new one | [Find active decisions and open questions](mcp/list_decisions.md) · [Follow evidence and history](mcp/get_decision.md) · [Record a decision](mcp/record_decision.md) · [Configure optional extraction](configuration.md#decision-extraction) |
| Connect an assistant or automate queries | [MCP client setup and tool reference](mcp/README.md) · [CLI commands](cli.md) · [HTTP API](requirements.md#http-api) |
| Administer a workspace | [Tokens and scopes](install.md#5-the-first-admin-token) · [Configuration](configuration.md) · [Security model](security.md) · [Forget a record](operations/runbook.md#forget-one-record) |
| Diagnose, back up or upgrade | [Installation and search troubleshooting](install.md#troubleshooting) · [Operations runbook](operations/runbook.md) · [Backup and restore](operations/backup.md) · [Performance](operations/performance.md) |

For interface contracts and design decisions, read the [requirements](requirements.md). The [implementation plan](plan.md) and [prior art](prior-art.md) describe the project's background.
