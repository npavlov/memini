# Memini

A self-hosted knowledge service for an engineer or a team lead. Code repositories, work messages and
meeting notes go in; an MCP server and an HTTP API come out.

It answers three kinds of questions:

- **Code**: where and how is X implemented, across many repositories.
- **Decisions**: what was decided about X, when, by whom, in which thread or meeting, and what it replaced.
- **My context**: which meetings did I attend about X, which messages were sent to me.

## Status

Milestone M0, the service skeleton. The service starts, applies its migrations, issues tokens and
answers the MCP `status` tool. It does not index or search anything yet: code search is M1.

- [Requirements](docs/requirements.md)
- [Implementation plan](docs/plan.md)
- [Prior art](docs/prior-art.md)

## Run from source

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker with compose.

```sh
uv sync
make db                                   # Postgres 18 with pgvector on 127.0.0.1:55432
export MEMINI_DATABASE_URL=postgresql://memini:memini@127.0.0.1:55432/memini

uv run memini migrate
uv run memini workspace create demo
TOKEN=$(uv run memini token create --scope read --name laptop)   # shown once
uv run memini serve                       # http://127.0.0.1:6364, MCP at /mcp
```

Connect an MCP client, for example Claude Code:

```sh
claude mcp add --transport http memini http://127.0.0.1:6364/mcp --header "Authorization: Bearer $TOKEN"
```

The service binds to `127.0.0.1` unless `MEMINI_HTTP_HOST` says otherwise. Health: `GET /healthz`;
readiness, with the database: `GET /readyz`; the HTTP API schema: `GET /openapi.json`. Logs are JSON
lines on stderr, with tokens and secrets masked.

### Commands

```
memini [--config FILE] [--debug] <command>
memini serve [--migrate]
memini migrate
memini workspace create <name> | list
memini token create --scope {read,ingest,admin} [--scope ...] [--workspace <name>] [--name <label>]
                    [--expires-in <duration>]
memini token list | revoke <id-or-prefix>
```

Settings come from `MEMINI_*` environment variables, or a TOML file named by `MEMINI_CONFIG_FILE`:
`MEMINI_DATABASE_URL` (or `MEMINI_DATABASE_URL_FILE`, a file that holds it), `MEMINI_HTTP_HOST`,
`MEMINI_HTTP_PORT`, `MEMINI_LOG_LEVEL`, `MEMINI_PRINCIPAL`. Secrets are read from the environment only,
never from the TOML file.

## Development database

`make db` runs `docker compose up -d --wait db`: Postgres 18 with pgvector, bound to `127.0.0.1:55432`.
The data stays in the `memini-db` volume across restarts. `MEMINI_DB_PORT` changes the host port.

## Contributing

Issues and pull requests are welcome. Setup, checks and project rules: [CONTRIBUTING.md](CONTRIBUTING.md).
Report a vulnerability privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
