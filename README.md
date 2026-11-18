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
and one Postgres database, started by Docker Compose and published on `127.0.0.1` only.
**[docs/install.md](docs/install.md)** goes from nothing to a search over a repository and a pushed
chat thread, step by step, with what each step shows and what to do when one fails.

Quickstart, with Docker (Compose 2.24.0 or later) and [Ollama](https://ollama.com) with
`qwen3-embedding:0.6b` pulled; on Linux, Ollama must listen beyond 127.0.0.1 (the guide says how):

```sh
git clone https://github.com/npavlov/memini.git
cd memini
cp .env.memini.example .env.memini
cat >> .env.memini <<'EOF'

# Embeddings: Ollama on this machine (docs/install.md)
MEMINI_EMBEDDING_PROVIDER=openai_compatible
MEMINI_EMBEDDING_URL=http://host.docker.internal:11434/v1
EOF
docker compose up -d --wait

export PATH="$PWD/bin:$PATH"     # bin/memini: the CLI, run inside the service's container
mkdir -p ~/.config/memini
(umask 077; memini bootstrap > ~/.config/memini/admin.token)
export MEMINI_TOKEN_FILE=~/.config/memini/admin.token
memini workspace create demo
memini source add demo memini --location https://github.com/npavlov/memini.git --ref main \
  --include 'docs/**' --include 'src/memini/cli/**' --schedule 1h
memini sync demo memini --wait
memini search "how is the first admin token created" --limit 3
```

`memini bootstrap` prints the first admin token once; every other command here is a client of the
HTTP API and reads its token from `MEMINI_TOKEN_FILE` (or `MEMINI_TOKEN`), never from the command
line. Give agents a token with the `read` scope only, for example in Claude Code:

```sh
(umask 077; memini token create --scope read --name claude-code > ~/.config/memini/read.token)
claude mcp add --scope user --transport http memini http://127.0.0.1:6364/mcp \
  --header "Authorization: Bearer $(cat ~/.config/memini/read.token)"
```

Or, in a client's JSON configuration:

```json
{"mcpServers": {"memini": {"type": "http", "url": "http://127.0.0.1:6364/mcp",
                           "headers": {"Authorization": "Bearer <token>"}}}}
```

Pushing messages and meeting notes, your own repositories, status and troubleshooting:
[docs/install.md](docs/install.md).

Backup and restore: `scripts/backup.sh` writes a pg_dump archive of the database, `scripts/restore.sh FILE`
restores it into an empty stack; mirrors are cloned again and the credential key is kept apart. See
[docs/operations/backup.md](docs/operations/backup.md).

Day to day:

| Task | Command |
|---|---|
| Logs (JSON lines; tokens and secrets masked) | `docker compose logs -f memini` |
| Stop, keeping the data | `docker compose stop` |
| Update to a new checkout | `git pull && docker compose up -d --wait` |
| Check it end to end | `make smoke` |
| Is the base current? (exit 6 if not) | `memini status --gate`, or `GET /v1/status?gate=true` (503) |
| Metrics (Prometheus) | set `MEMINI_METRICS_TOKEN_FILE` in `.env`, scrape `GET /metrics` with it as bearer |

Health: `GET /healthz`; readiness, with the database: `GET /readyz`; the HTTP API schema:
`GET /openapi.json`.

### Embeddings

Search and git sync need an embedding model. The default is Qwen3-Embedding 0.6B, served by
[Ollama](https://ollama.com) on your machine; no text leaves it.

1. Install Ollama and pull the model (about 640 MB): `ollama pull qwen3-embedding:0.6b`
2. Copy `.env.memini.example` to `.env.memini`, uncomment `MEMINI_EMBEDDING_PROVIDER=openai_compatible`
   and `MEMINI_EMBEDDING_URL=http://host.docker.internal:11434/v1`, then `docker compose up -d --wait`.

That one setting implies the rest: the model, 1024 dimensions, the pinned Ollama digest and the
query instruction. In compose the service reaches Ollama at `http://host.docker.internal:11434/v1`;
a service run from source uses `http://127.0.0.1:11434/v1`. `.env.memini.example` lists every value. On
Linux, Ollama listens on 127.0.0.1 only: set `OLLAMA_HOST=0.0.0.0:11434` for its service and keep
port 11434 closed to other machines. Without a provider the service still starts, but it does not
sync git sources and search answers `embeddings_not_configured`. Changing the model, dimension,
digest or document prefix changes the index fingerprint: run `memini reindex --workspace NAME`
afterwards.

Optional reranking: Qwen3-Reranker 0.6B in the process, off by default. Install
`memini[local-embeddings]` and set `MEMINI_SEARCH_RERANKER=local`; on the code suite it lifts hybrid
R@1 from 0.61 to 0.78 for about 0.5 s per search. `memini eval --reranker local` measures it.

MCP sources: the service syncs MCP servers on a schedule (`memini source add <ws> <name> --kind mcp
--url URL --recipe PATH --stream KEY`); list their servers in `MEMINI_MCP_ALLOWED_ENDPOINTS` or
`MEMINI_MCP_ALLOWED_COMMANDS`. Internal addresses are refused unless `MEMINI_MCP_ALLOWED_NETWORKS` lists
them.

Agent executor: bring an MCP source that Memini cannot reach on its own into Memini from a Claude Code
session, with the `memini-intake` skill and capture hook in `integrations/claude-code`
([docs/recipes.md](docs/recipes.md), Agent executor).

### Tools

Over MCP, and over HTTP with the same arguments as query parameters. All need a token with the
`read` scope and see only the workspaces the token may read.

| MCP tool | HTTP | What it does |
|---|---|---|
| `search` | `GET /v1/search` | Hybrid search over everything: code, and Markdown files of repositories |
| `search_code` | `GET /v1/search/code` | Code search: `semantic`, `exact`, `regex` or `symbol`; filters by repository, path glob, language |
| `get_symbol` | `GET /v1/symbols` | Definitions of a symbol, by name or qualified name |
| `read_source` | `GET /v1/workspaces/{ws}/sources/{src}/files/{path}` | A file or line range, or a directory listing |
| `list_meetings` | `GET /v1/meetings` | Meetings a person took part in (default: me), by time and topic words |
| `get_meeting` | `GET /v1/meetings/{record_id}` | Title, participants, summary, action items; transcript on request |
| `list_messages` | `GET /v1/messages` | Messages to, mentioning or from a person, one by one, by source, time and topic |
| `get_thread` | `GET /v1/threads/{record_id}` | A whole thread, messages in order |
| `add_document` | `POST /v1/workspaces/{ws}/sources/{src}/documents` | Store a document in a push source (ingest scope) |
| `status` | | Stream lag, last runs, and whether the base is current |

Every hit carries its source, a timestamp and a locator: `shop@3f2a…:src/app.py#L12-L30` for code,
`chat:general/1772323200.000100#msg-1772323260.000200` for a message, with a `link` to it and an
`anchor` that `record_decision` takes as evidence.
`read_source` reads the service's own mirror at the pinned commit by default, so it matches search
results; `ref` takes a branch, a tag or a full commit id. Search needs an embedding provider
(`MEMINI_EMBEDDING_*`); reading files does not. An `index_mismatch` error means the embedding model or
the chunker changed: run `docker compose exec memini memini reindex --workspace NAME`. Over HTTP, send
a time zone offset's `+` as `%2B`, or use `Z`.

Repositories are added with `memini source add`, which calls `POST /v1/workspaces/{ws}/sources` with an
`admin` token (see `/openapi.json`); the service mirrors and indexes them on their schedule, or at
`memini sync` (`POST /v1/workspaces/{ws}/sources/{src}/sync`).

### Commands

Client commands call the HTTP API of the running service. Run them with `bin/memini …`, the CLI inside
the service's container, with the token from `MEMINI_TOKEN_FILE` or `MEMINI_TOKEN` on the host
([docs/install.md](docs/install.md)); or on the host with `uv run memini …` in the checkout, with
`MEMINI_URL` and `MEMINI_TOKEN` or `MEMINI_TOKEN_FILE`, where `--url URL` and `--token-file FILE`
override them for one run. Reading needs a token with the `read` scope, changing
anything `admin`. Read commands take `--json` for the API's answer as it is.

```
memini workspace create <name> | list
memini source add <ws> <name> --location URL_OR_PATH --ref REF [--ref ...] [--schedule DURATION]
                  [--include GLOB ...] [--exclude GLOB ...] [--no-builtin-excludes] [--visibility V]
                  [--retention-days N] [--llm-policy P]
memini source add <ws> <name> --kind push [--visibility V] [--retention-days N] [--llm-policy P]
memini source list <ws> | show <ws> <source> | remove <ws> <source>
memini source edit <ws> <source> [--name NEW] [--ref REF ...] [--schedule DURATION | --no-schedule] ...
memini streams <ws> <source>                     # lag, last run and queued sync of each ref
memini sync <ws> <source> [--stream KEY ...] [--wait]
memini runs <ws> [--source NAME] [--stream KEY] [--limit N] [--before CURSOR]
memini run <run-id>                              # one run with its report
memini token create --scope {read,ingest,admin} [--scope ...] [--workspace NAME] [--name LABEL]
                    [--expires-in DURATION]      # prints the token alone, once
memini token list | revoke <id-or-prefix>
memini search <query> [--code] [--mode MODE] [--workspace NAME] [--repository NAME] [--limit N]
memini decision record <ws> --statement TEXT [--kind decision|open_question] [--decided-at TIME]
                       --evidence SOURCE:EXTERNAL_ID[#ANCHOR] [--participant PERSON_ID ...]
                       [--supersedes ITEM_ID[:replaces|answers] ...]   # needs the ingest scope
memini decision supersede <ws> <item-id> --supersedes ITEM_ID[:replaces|answers] [...]
memini decision close|retract <ws> <item-id>
memini extract <ws> [--source NAME ...] [--since TIME] [--dry-run]   # extract decisions from stored
                    records the current extractor has not read (needs the admin scope)
```

Operator commands work on the database directly, so they run inside the service's container:
`docker compose exec memini memini …`, or `bin/memini …`. They come before a token exists, or below
the API.

```
memini serve [--migrate]
memini migrate
memini bootstrap [--name LABEL] [--expires-in DURATION] [--force]   # the first admin token
memini workspace llm <name> [none|local|cloud]   # which LLM may read a workspace; cloud is an opt-in
memini reindex --workspace <name>
memini replay --workspace <name> --source <name> --recipe PATH   # after a parser fix, no source calls
memini eval [--suite PATH] [--baseline PATH [--write-baseline]] [--fake-embeddings] ...
memini credentials set|list|delete|rotate-key ...   # credentials of sources, encrypted; secrets on stdin
```

`workspace llm` sits in the `workspace` group but is an operator command: no token, an admin one
included, may opt a workspace in to a cloud LLM, so the HTTP API has no route for it.

`bootstrap` gives the principal `MEMINI_PRINCIPAL` (default `owner`) a token with the `read` and `admin`
scopes for every workspace. It refuses while that principal has such a token that is active; with
`--force` it makes another one, for an operator who lost theirs (revoke the old one with
`memini token revoke`).

Exit codes: 0 done; 1 failed (no such thing, refused by the service, a failed run waited for); 2 wrong
usage or configuration (no token, a bad URL); 3 the service or the database cannot be reached; 4 `eval`
only: a metric fell below the baseline; 5 the service refused the token (unknown, revoked, expired, or
without the scope or workspace); 130 interrupted. Errors are one line on stderr, with the API's error
code and request id.

### Settings

Every setting, its default and what a change needs (restart or reindex):
[docs/configuration.md](docs/configuration.md), generated by `make config-docs`.

Settings come from `MEMINI_*` environment variables, or a TOML file named by `MEMINI_CONFIG_FILE`:
`MEMINI_DATABASE_URL` (or `MEMINI_DATABASE_URL_FILE`, a file that holds it), `MEMINI_HTTP_HOST`,
`MEMINI_HTTP_PORT`, `MEMINI_LOG_LEVEL`, `MEMINI_PRINCIPAL`; the embedding provider, `MEMINI_EMBEDDING_*`
(off until `MEMINI_EMBEDDING_PROVIDER` is set); the git connector, `MEMINI_GIT_*` (mirror directory,
limits, allowed transports, an https token or an ssh key); search, `MEMINI_SEARCH_*` (timeout, candidates,
rank fusion constant). Secrets are read from the environment only,
never from the TOML file. With compose, `.env` (`.env.example`) sets the published ports, the database
password and the paths of secret files; every setting of the service goes into `.env.memini`
(`.env.memini.example`), and keys and tokens into files mounted as compose secrets
([docs/configuration.md](docs/configuration.md), "Docker compose"). Needs Docker Compose 2.24.0 or later.

## Develop

Running from source, the tests and the project rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Issues and pull requests are welcome. Setup, checks and project rules: [CONTRIBUTING.md](CONTRIBUTING.md).
Report a vulnerability privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
