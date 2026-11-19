# Install Memini

From nothing to a search over one repository and one chat thread, with Docker Compose. It takes
about a quarter of an hour, most of it the first build of the image and the download of the
embedding model. Run the steps in order; each one says what you should see.

The result: the service and its database run on this machine, published on `127.0.0.1` only; one
workspace holds a public repository and a pushed chat thread; Claude Code (or another MCP client)
searches them with a read-only token.

## What you need

- **Docker with Compose 2.24.0 or later.** Docker Desktop or OrbStack on macOS, Docker Engine with
  the compose plugin on Linux. Check: `docker compose version`.
- **git**, to clone the repository. Optionally **curl** 7.55.0 or later (`curl --version`), for the
  curl form of step 7.
- **[Ollama](https://ollama.com)**, which runs the embedding model on this machine: no text leaves
  it. Install it, then pull the model (about 640 MB):

  ```sh
  ollama pull qwen3-embedding:0.6b
  ```

  On Linux, Ollama listens on 127.0.0.1 only, which a container cannot reach. Let it listen on
  every interface: set `OLLAMA_HOST=0.0.0.0:11434` in the environment of its service (with systemd:
  `sudo systemctl edit ollama.service`, add `Environment="OLLAMA_HOST=0.0.0.0:11434"` under
  `[Service]`, then `sudo systemctl restart ollama`), and keep port 11434 closed to other machines
  with a firewall. Docker Desktop and OrbStack reach Ollama on the host's 127.0.0.1 without this.
- **Disk and memory:** see [Machine size](#machine-size).
- Optional: Claude Code or another MCP client, for step 8.

You need no Python and no uv on the host: the `memini` command runs inside the service's container
(step 4).

## 1. Clone

```sh
git clone https://github.com/npavlov/memini.git
cd memini
```

Every command below runs in this directory, unless it says otherwise.

## 2. Settings

Two files, both ignored by git; [configuration.md](configuration.md), "Docker compose", describes
them.

`.env` is what compose itself reads: the ports on the host, the database password and the paths of
secret files. Every line is optional; set a database password now, as the database keeps the one it
is created with:

```sh
cp .env.example .env
printf 'MEMINI_DB_PASSWORD=%s\n' "$(openssl rand -hex 16)" >> .env
```

The service is published on `127.0.0.1:6364` and the database on `127.0.0.1:55432`. If either port
is taken on this machine, add `MEMINI_HTTP_PORT=…` or `MEMINI_DB_PORT=…` to `.env`, and use your
HTTP port wherever this guide says 6364.

`.env.memini` holds the settings of the service. Turn on embeddings with Ollama on this machine,
which compose reaches as `host.docker.internal`:

```sh
cp .env.memini.example .env.memini
cat >> .env.memini <<'EOF'

# Embeddings: Ollama on this machine (docs/install.md)
MEMINI_EMBEDDING_PROVIDER=openai_compatible
MEMINI_EMBEDDING_URL=http://host.docker.internal:11434/v1
EOF
```

That one provider implies the model (`qwen3-embedding:0.6b`), its dimension and its query
instruction. Without it the service starts, but syncs no repository and answers every search with
`embeddings_not_configured`.

## 3. Start

```sh
docker compose up -d --wait
```

The first time, this builds the service's image from the checkout and pulls Postgres: a few
minutes. The build fetches the reranker's model files from Hugging Face, about 610 MB, checks them
against their pinned SHA-256 and keeps them in the image, so the service never fetches them; later
builds take them from Docker's build cache. It returns when the database and the service are
healthy and the migrations are applied.
`docker compose ps` shows both containers `(healthy)`. They restart with Docker until you stop them.

## 4. The `memini` command

`bin/memini` runs the `memini` command inside the service's container, with Docker only. Put it on
your `PATH` for this shell:

```sh
export PATH="$PWD/bin:$PATH"
memini --version
```

It prints `memini` and a version. To keep the command in new shells, add that `export` line, with
the full path of `bin`, to your shell's profile. `bin/memini` finds this checkout's stack from any
directory.

Most commands are clients of the service's HTTP API and need a token, read from the file
`MEMINI_TOKEN_FILE` names (or from `MEMINI_TOKEN`); no option takes the token itself. A few
operator commands, such as `bootstrap` and `reindex`, work on the database directly and need none.
With [uv](https://docs.astral.sh/uv/) installed, the same CLI also runs on the host, as
`uv run memini …` in the checkout (CONTRIBUTING.md); then set `MEMINI_URL` if the port is not 6364.

## 5. The first admin token

`memini bootstrap` makes the first token with the `admin` and `read` scopes. It is printed once:
keep it in a file only you can read, and point the CLI at it:

```sh
mkdir -p ~/.config/memini
(umask 077; memini bootstrap > ~/.config/memini/admin.token)
export MEMINI_TOKEN_FILE=~/.config/memini/admin.token
memini workspace list
```

`bootstrap` says on stderr which token it created, with its prefix; `workspace list` says there are
no workspaces yet.

## 6. A workspace and a repository

A workspace holds sources; a token can be limited to one. Create one, then add a repository by its
https URL, the tracked branch and how often to sync it. Any public repository works; this one is
Memini itself, its documentation and its CLI only (`--include`), about 30 files:

```sh
memini workspace create demo
memini source add demo memini --location https://github.com/npavlov/memini.git --ref main \
  --include 'docs/**' --include 'src/memini/cli/**' --schedule 1h
memini sync demo memini --wait
```

The service clones the repository into its own mirror, chunks the files and embeds them with
Ollama. `sync --wait` ends with `refs/heads/main: run … succeeded` after half a minute or so; a
larger repository takes longer. Leave out `--include` to index every file. `sync --wait` waits 10
minutes at most; a sync that takes longer goes on in the service: wait longer with
`--wait-timeout 1h`, or follow it with `memini streams demo memini`. Then search it:

```sh
memini search "how is the first admin token created" --limit 3
```

The first hits are in `src/memini/cli/admin.py`, each with its locator
(`memini@<commit>:src/memini/cli/admin.py#L…`) and the first lines of the chunk.

### Your own repositories

A repository on this machine is a source once the service can see it: mount the directory that
holds your repositories into the container, read-only, and allow that path. Say your repositories
are under `~/src`; `compose.override.yaml` (ignored by git) adds the mount to `compose.yaml`:

```sh
cat > compose.override.yaml <<'EOF'
services:
  memini:
    volumes:
      - ${HOME}/src:/srv/repos:ro
EOF
echo 'MEMINI_GIT_LOCAL_ROOTS=/srv/repos' >> .env.memini
docker compose up -d --wait
memini source add demo myapp --location /srv/repos/myapp --ref main
memini sync demo myapp --wait
```

`/srv/repos/myapp` is `~/src/myapp` as the container sees it. On Linux the files must be readable by
the service's user, uid 10001 (they are, with the usual modes 644 and 755). A private remote
repository needs a token or an ssh key: [configuration.md](configuration.md), "Git connector" and
"Secrets and other files".

## 7. Push a chat thread

Messages, mail and meeting notes come in through the push API: a push source takes the records
that a client or an agent sends it. Add one, and make a token that may only push into this
workspace (scope `ingest`), kept in a file only you can read. Then push the example thread with it:

```sh
memini source add demo chat --kind push
(umask 077; memini token create --scope ingest --workspace demo --name push > ~/.config/memini/ingest.token)
MEMINI_TOKEN_FILE=~/.config/memini/ingest.token memini push --workspace demo --source chat - < docs/examples/thread.json
```

`memini push` reads the records from stdin (`-`): `bin/memini` runs the command inside the
service's container, which cannot read a path on the host. It prints:

```
sent 1 record in 1 batch to demo/chat (stream chat/platform)
stored 1 (inserted 1, updated 0, revived 0), deleted 0, unchanged 0
```

[`docs/examples/thread.json`](examples/thread.json) is one batch with one invented thread. Pushing
it again changes nothing (`unchanged 1`). Without an embedding provider a third line says the
thread was stored but not indexed (see Troubleshooting). The shapes of chat and mail threads,
meetings and documents, with an example of each, and the rules for an agent that pushes what it
reads: [intake.md](intake.md).

The same batch with curl instead, which reads the header with the token from a file, so the token
is on no command line:

```sh
(umask 077; printf 'Authorization: Bearer %s\n' "$(cat ~/.config/memini/ingest.token)" > ~/.config/memini/ingest.header)
curl -sS -X POST -H @"$HOME/.config/memini/ingest.header" -H 'Content-Type: application/json' \
  --data-binary @docs/examples/thread.json \
  http://127.0.0.1:6364/v1/workspaces/demo/sources/chat/records
```

Its answer counts the thread (`"unchanged":1` after the push above), and
`"index":{"indexed":true,…}` says its chunks are embedded. Search everything:

```sh
memini search "when does the nightly export run" --limit 3
```

The first hit is the thread, `chat:platform/1793610720.000100#msg-…`, with its link and messages.

## 8. Connect Claude Code

Agents get a token with the `read` scope only. Make one, and add Memini to Claude Code for all your
projects:

```sh
(umask 077; memini token create --scope read --name claude-code > ~/.config/memini/read.token)
claude mcp add --scope user --transport http memini http://127.0.0.1:6364/mcp \
  --header "Authorization: Bearer $(cat ~/.config/memini/read.token)"
claude mcp list
```

`claude mcp list` shows `memini: http://127.0.0.1:6364/mcp (HTTP) - ✔ Connected`. Claude Code keeps
the token in its own configuration file. Start Claude Code and ask, for example: *What did the
platform channel decide about the nightly export? Use the memini search tool.* It calls `search` and
answers from the thread: 04:30 UTC, a 45-minute timeout.

Another MCP client takes the same URL and header, e.g. in its JSON configuration:

```json
{"mcpServers": {"memini": {"type": "http", "url": "http://127.0.0.1:6364/mcp",
                           "headers": {"Authorization": "Bearer <token>"}}}}
```

The tools and their arguments: [README.md](../README.md), "Tools".

## 9. Status

```sh
memini status
```

The first lines say whether the base is current, the database's schema version and the index: `ok`,
built with `qwen3-embedding:0.6b`. Below, one line per stream: the repository's branch and the
chat channel, with the last result, the lag and whether each is current. `memini status --gate`
exits 0 when everything is current and 6 when not, for scripts and health checks.

## Day to day

| Task | Command |
|---|---|
| Logs (JSON lines; tokens and secrets masked) | `docker compose logs -f memini` |
| Stop, keeping the data | `docker compose stop` |
| Start again | `docker compose up -d --wait` |
| Update to a new checkout | `git pull && docker compose up -d --wait` |
| Tokens: list, revoke | `memini token list`, `memini token revoke <id-or-prefix>` |
| Remove the stack **and every piece of data** | `docker compose down --volumes` |

Back up the database with `scripts/backup.sh`: [operations/backup.md](operations/backup.md). Every
setting, and what a change of it needs: [configuration.md](configuration.md). Every command:
`memini --help`, and [README.md](../README.md), "Commands".

## Machine size

Rough figures from following this guide on a laptop (Apple M4 Max, Ollama on its GPU). The
performance baseline states the smallest supported machine here, with measured numbers.

- **Disk:** the images, about 1.6 GB (the service 1.1 GB, of which 0.61 GB is the reranker's model;
  Postgres with pgvector 0.49 GB), and the embedding model, 0.64 GB. The database grows with what
  is indexed: about 110 MB after steps 6 and 7, most of it Postgres's own files; about 400 MB with
  the whole Memini repository indexed as well.
- **Memory:** the service about 1.1 GB, nearly all of it the reranker's model (about 100 MB with
  `MEMINI_SEARCH_RERANKER=none` in `.env.memini`); the database under 200 MB while indexing and
  about 80 MB at rest. Ollama with the model loaded: about 1 GB, more with the longer context Ollama
  gives it on a machine with much memory.
- **CPU:** reranking adds 0.5 to 0.7 s to a search with 4 CPUs, 1.2 s with 2 and 2.2 s with 1
  (requirements.md, section 9, Reranker).
- **Time:** indexing is the model's work. The sync of step 6, 28 files and 460 chunks, took 28 s;
  the whole Memini repository, 518 files and about 7,300 chunks, 4.5 minutes. On a CPU only, expect
  several times longer.

## Troubleshooting

**`reranking is off: …`** in the log (`docker compose logs memini`). Search works without the
reranker, in the order of its retrievers; the line says why. `the files of model … are not in
/opt/memini/models/…, and it cannot be created`: the image was built without them
(`MEMINI_RERANKER_FILES=none` in `.env`): remove that line and `docker compose up -d --wait`, or set
`MEMINI_SEARCH_RERANKER=none` in `.env.memini` to keep it off without the warning. Another model
named in `.env.memini` needs a writable `MEMINI_SEARCH_RERANKER_MODEL_DIR`
([configuration.md](configuration.md)). Once it works, the log says
`reranking with n24q02m/Qwen3-Reranker-0.6B-ONNX …, 4 threads`.

**`docker: 'compose' is not a docker command`, or `docker compose up` fails on `env_file` in
`compose.yaml`.** Compose is missing or older than 2.24.0, which reads an optional `env_file`. Check
`docker compose version`; update Docker Desktop, or install the current compose plugin on Linux.

**`Bind for 127.0.0.1:55432 failed: port is already allocated`** (or `:6364`). Another program, or
another Memini, holds the port. Set `MEMINI_DB_PORT` or `MEMINI_HTTP_PORT` in `.env` to a free port
and run `docker compose up -d --wait` again; use your HTTP port in the URLs of steps 7 and 8.
`memini` needs no change.

**`service "memini" is not running`** from `memini`. The stack is stopped: `docker compose up -d
--wait` in the checkout.

**`embeddings_not_configured`** from a search; `index off` in `memini status`; or `sync --wait` says
`still queued after 30s, not started`. No embedding provider is set: add the two lines of step 2 to
`.env.memini` and run `docker compose up -d --wait`. The queued sync then runs. A thread pushed
meanwhile is stored without its index: `memini reindex --workspace demo` indexes it.

**`embedding_failed`** from a search or a push, or a failed sync, and the log
(`docker compose logs memini`) says `embedding request to http://host.docker.internal:11434 failed
(ConnectError: …)`. The service cannot reach Ollama. Check that Ollama runs and has the model
(`ollama list` on the host), then that the container reaches it:

```sh
docker compose exec memini python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/version').read().decode())"
```

It prints Ollama's version, or `Connection refused` when Ollama listens on 127.0.0.1 only: on Linux,
set `OLLAMA_HOST` as in [What you need](#what-you-need).

**`index_mismatch`**: `The index of workspace demo is out of date … run memini reindex --workspace
demo`. The embedding model, its settings or the chunker changed since the workspace was indexed;
search and indexing there are refused until you rebuild it:

```sh
memini reindex --workspace demo
```

**`memini: no token: set MEMINI_TOKEN or MEMINI_TOKEN_FILE`** in a new shell: export
`MEMINI_TOKEN_FILE` again (step 5). Exit code 5 with `invalid_token, HTTP 401`: the token is unknown
or revoked; with `insufficient_scope, HTTP 403`: it lacks the scope, e.g. a read token on
`workspace create`.

**`memini bootstrap`: `an active admin token exists`.** There is an admin token already, and
bootstrap makes only the first. If you lost it, make another, then revoke the old one by the prefix
`memini token list` shows for it:

```sh
(umask 077; memini bootstrap --force > ~/.config/memini/admin.token)
memini token list
memini token revoke <prefix>
```

**The service restarts, and `docker compose up --wait` says `container … is unhealthy`;** its log
says `MEMINI_…_FILE: cannot read /run/secrets/…: Permission denied`. The service runs as uid 10001
and must read every secret file named in `.env`. On Linux a file keeps its owner and mode in the
container: `sudo chown 10001 secrets/FILE && sudo chmod 400 secrets/FILE`, then
`docker compose up -d --wait`.

**A local repository is refused:** `local repositories are not allowed as sources` or `outside the
directories in MEMINI_GIT_LOCAL_ROOTS`. Set `MEMINI_GIT_LOCAL_ROOTS` in `.env.memini`, mount the
directory as in [Your own repositories](#your-own-repositories), and give `--location` as the path
inside the container.

**`claude mcp list` does not show `Connected`.** Check the port in the URL and that the token in the
header is the one in `read.token` and not revoked (`memini token list`); `claude mcp remove memini
--scope user` and add it again.
