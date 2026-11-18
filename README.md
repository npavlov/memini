# Memini

A self-hosted knowledge service for an engineer or a team lead. Code repositories, work messages and
meeting notes go in; an MCP server and an HTTP API come out.

It answers three kinds of questions:

- **Code**: where and how is X implemented, across many repositories.
- **Decisions**: what was decided about X, when, by whom, in which thread or meeting, and what it replaced.
- **My context**: which meetings did I attend about X, which messages were sent to me.

## Status

Milestone M0, the service skeleton, is done: the service runs under `docker compose`, applies its
migrations, issues tokens and answers the MCP `status` tool. It does not index or search anything yet:
code search is M1.

- [Requirements](docs/requirements.md)
- [Implementation plan](docs/plan.md)
- [Prior art](docs/prior-art.md)

## Run the service

Memini runs as a long-lived service: one process (HTTP API, MCP endpoint, job worker and scheduler)
and one Postgres database, started by Docker Compose. It needs Docker with compose.

```sh
git clone https://github.com/npavlov/memini.git
cd memini
docker compose up -d --wait
```

This builds the image from the checkout, starts Postgres and the service, applies the migrations and
returns once both are healthy. The containers restart on failure and with Docker, unless you stop
them. The service is published on `127.0.0.1:6364` only; the database on `127.0.0.1:55432`. Data lives
in the `memini-db` volume.

Create a workspace and a token with the CLI inside the container. The token is printed once:

```sh
docker compose exec memini memini workspace create demo
TOKEN=$(docker compose exec -T memini memini token create --scope read --name laptop)
```

Connect an MCP client, for example Claude Code:

```sh
claude mcp add --transport http memini http://127.0.0.1:6364/mcp --header "Authorization: Bearer $TOKEN"
```

Or, in a client's JSON configuration:

```json
{"mcpServers": {"memini": {"type": "http", "url": "http://127.0.0.1:6364/mcp",
                           "headers": {"Authorization": "Bearer <token>"}}}}
```

Day to day:

| Task | Command |
|---|---|
| Logs (JSON lines; tokens and secrets masked) | `docker compose logs -f memini` |
| Stop, keeping the data | `docker compose stop` |
| Update to a new checkout | `git pull && docker compose up -d --wait` |
| Check it end to end | `make smoke` |

Health: `GET /healthz`; readiness, with the database: `GET /readyz`; the HTTP API schema:
`GET /openapi.json`.

### Tools

Over MCP, and over HTTP with the same arguments as query parameters. All need a token with the
`read` scope and see only the workspaces the token may read.

| MCP tool | HTTP | What it does |
|---|---|---|
| `search` | `GET /v1/search` | Hybrid search over everything: code, and Markdown files of repositories |
| `search_code` | `GET /v1/search/code` | Code search: `semantic`, `exact`, `regex` or `symbol`; filters by repository, path glob, language |
| `get_symbol` | `GET /v1/symbols` | Definitions of a symbol, by name or qualified name |
| `read_source` | `GET /v1/workspaces/{ws}/sources/{src}/files/{path}` | A file or line range, or a directory listing |
| `status` | | Stream lag, last runs, and whether the base is current |

Every hit carries its source, a timestamp and, for code, a locator such as `shop@3f2a…:src/app.py#L12-L30`.
`read_source` reads the service's own mirror at the pinned commit by default, so it matches search
results; `ref` takes a branch, a tag or a full commit id. Search needs an embedding provider
(`MEMINI_EMBEDDING_*`); reading files does not. An `index_mismatch` error means the embedding model or
the chunker changed: run `memini reindex --workspace NAME`. Over HTTP, send a time zone offset's `+` as
`%2B`, or use `Z`.

Repositories are added through the HTTP API: `POST /v1/workspaces/{ws}/sources` with an `admin` token
(see `/openapi.json`); the service mirrors and indexes them on their schedule, or at
`POST /v1/workspaces/{ws}/sources/{src}/sync`.

### Commands

Run them with `docker compose exec memini memini …`.

```
memini [--config FILE] [--debug] <command>
memini serve [--migrate]
memini migrate
memini workspace create <name> | list
memini token create --scope {read,ingest,admin} [--scope ...] [--workspace <name>] [--name <label>]
                    [--expires-in <duration>]
memini token list | revoke <id-or-prefix>
memini reindex --workspace <name>
```

### Settings

Settings come from `MEMINI_*` environment variables, or a TOML file named by `MEMINI_CONFIG_FILE`:
`MEMINI_DATABASE_URL` (or `MEMINI_DATABASE_URL_FILE`, a file that holds it), `MEMINI_HTTP_HOST`,
`MEMINI_HTTP_PORT`, `MEMINI_LOG_LEVEL`, `MEMINI_PRINCIPAL`; the embedding provider, `MEMINI_EMBEDDING_*`
(off until `MEMINI_EMBEDDING_PROVIDER` is set); the git connector, `MEMINI_GIT_*` (mirror directory,
limits, allowed transports, an https token or an ssh key); search, `MEMINI_SEARCH_*` (timeout, candidates,
rank fusion constant). Secrets are read from the environment only,
never from the TOML file. With compose, `MEMINI_HTTP_PORT`, `MEMINI_DB_PORT`, `MEMINI_DB_PASSWORD` and
`MEMINI_LOG_LEVEL` in the shell or a `.env` file change the published ports, the database password and
the log level.

## Develop

Running from source, the tests and the project rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Issues and pull requests are welcome. Setup, checks and project rules: [CONTRIBUTING.md](CONTRIBUTING.md).
Report a vulnerability privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
