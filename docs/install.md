# Install Memini

<nav class="install-paths" aria-label="Installation options">
<a href="#what-you-need"><strong>Run a local server</strong><span>Install the launcher, then start Memini with Docker.</span></a>
<a href="install-cli.md"><strong>Connect to a server</strong><span>Use an existing server address and a read token.</span></a>
</nav>

## What you need

- **macOS or Linux:** [Homebrew](https://brew.sh/) and a running Docker engine.
- **Windows x64:** PowerShell, [Scoop](https://scoop.sh/) and Docker Desktop running Linux containers.
- **Hardware:** 4 CPU cores, 8 GB RAM and 20 GB free SSD. A GPU is optional.

Run `docker compose version` to check for Compose **2.24 or newer**, then `docker info` to check that the engine is running.

## Install the server launcher

### macOS and Linux

```sh
brew tap npavlov/memini https://github.com/npavlov/memini.git
brew install npavlov/memini/memini-server
```

### Windows

In PowerShell:

```powershell
scoop bucket add memini https://github.com/npavlov/memini.git
scoop install memini/memini-server
```

Add the tap or bucket once. The launcher includes the release settings and sample records. For a Compose installation without a package manager, use the [manual ZIP instructions](#install-a-published-image-with-docker-only).

## 3. Start the server

```sh
memini-server start
memini-server status
```

The first start generates a database password and waits for Memini, PostgreSQL and Ollama. When the three services are healthy, use the address printed by the launcher, normally `http://127.0.0.1:6364`. A later start reuses the same settings and data.

![Terminal showing healthy checks for db, memini and ollama](assets/install-health.png)

## 4. The `memini` command

`memini-server cli` runs the installed server's CLI inside its container. These shortcuts apply to the current shell. They let you use `memini` in the examples below and locate the bundled sample files.

**Bash or Zsh**

```sh
memini() { memini-server cli "$@"; }
MEMINI_RESOURCES="$(memini-server path)"
memini --version
```

**PowerShell**

```powershell
function memini { memini-server cli @args }
$MeminiResources = memini-server path
memini --version
```

## 5. The first admin token

Save the first token to a private file. If you already bootstrapped this server, reuse its token file and skip creating a new one.

**Bash or Zsh**

```sh
mkdir -p "$HOME/.config/memini"
( set -C; umask 077; memini bootstrap > "$HOME/.config/memini/admin.token" )
export MEMINI_TOKEN_FILE="$HOME/.config/memini/admin.token"
memini workspace list
```

**PowerShell**

This helper reserves a new file before issuing a token and saves it as ASCII, which works in both Windows PowerShell and PowerShell 7. It refuses an existing file. Keep the helper in this shell for the first search example.

```powershell
function Save-MeminiToken($Path, [scriptblock]$Issue) {
    $ErrorActionPreference = 'Stop'
    $file = [IO.File]::Open($Path, 'CreateNew', 'Write', 'None')
    try {
        $token = & $Issue
        if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Token creation failed.' }
        $bytes = [Text.Encoding]::ASCII.GetBytes($token.Trim())
        $file.Write($bytes, 0, $bytes.Length)
    } finally {
        $empty = $file.Length -eq 0
        $file.Dispose()
        if ($empty) { [IO.File]::Delete($Path) }
    }
}
$tokenDir = Join-Path $HOME '.config/memini'
[IO.Directory]::CreateDirectory($tokenDir) | Out-Null
$adminFile = Join-Path $tokenDir 'admin.token'
Save-MeminiToken $adminFile { memini-server cli bootstrap }
$env:MEMINI_TOKEN_FILE = $adminFile
memini workspace list
```

A new server reports an empty workspace list. **[Import a sample email and run your first search](first-search.md)** from Bash or PowerShell. The remaining shell examples on this page use Bash.

## Connect to an existing server

Ask the operator for the server URL and a read token file. The [client installation guide](install-cli.md) covers the Windows x64 installer and Homebrew for Apple Silicon macOS and x86_64 Linux, with PowerShell and Bash examples. Run `memini workspace list` to check your connection. To develop Memini itself, use [Build from source](build-from-source.md).

## 6. A workspace and a repository

A workspace holds sources; a token can be limited to one. Create one, then add a repository by its
https URL, the tracked branch and how often to sync it. Any public repository works; this one is
Memini itself, limited to its bootstrap CLI file so the first sync is small on CPU-only machines:

```sh
memini workspace create demo
memini source add demo memini --location https://github.com/npavlov/memini.git --ref refs/tags/v0.1.0 \
  --include 'src/memini/cli/admin.py' --schedule 1h
memini sync demo memini --wait --wait-timeout 45m
```

The service clones the repository into its own mirror, chunks the files and embeds them with
Ollama. `sync --wait` ends with `run … succeeded` for the selected ref; a
larger repository takes longer. Leave out `--include` to index every file. `sync --wait` waits 10
minutes by default (the quickstart allows 45); a sync that takes longer goes on in the service: wait longer with
`--wait-timeout 1h`, or follow it with `memini streams demo memini`. Then search it:

```sh
memini search "how is the first admin token created" --limit 3
```

The first hits are in `src/memini/cli/admin.py`, each with its locator
(`memini@<commit>:src/memini/cli/admin.py#L…`) and the first lines of the chunk.

### Your own repositories

Use a repository's HTTPS URL or SSH remote, or a folder of this computer. The launcher gives a
managed server both the folders and the access to private remotes, as below; the desktop app does
the same on its Repositories and Server screens, without a terminal. For a custom Compose setup,
see [Git connector settings and credentials](configuration.md#git-connector) and
[local repositories](configuration.md#local-repositories).

### Repositories and git access

**A folder of this computer.** Give it to the server, then add the git source at the location the
launcher prints:

```sh
memini-server repos add /Users/sam/src/harbour
# harbour: /Users/sam/src/harbour as /srv/local/harbour
memini source add demo harbour --location /srv/local/harbour --ref main
memini-server repos list
memini-server repos remove harbour
```

The launcher keeps the folders in `local-repos.json` in its state directory. It writes
`compose.local.yaml` there, which mounts each folder read-only at `/srv/local/<name>` and sets
`MEMINI_GIT_LOCAL_ROOTS=/srv/local`, and it recreates the running service. The server reads the
folder's git history and never writes to it. It sees new commits once they are committed or
pulled in that folder.

Check the folder's place before you add it:

- Docker Desktop shows a container only the folders listed under **Settings → Resources → File
  sharing**. A folder outside them, such as a disk under `/Volumes` taken out of the list, is
  mounted empty, or not at all. The launcher looks into the mount, and when it is empty it takes the
  folder back and says so.
- On Linux the service runs as uid 10001 and must be able to read the folder.
- A linked worktree or a submodule has a `.git` file that points elsewhere, so add the repository
  it belongs to.

**A private remote.** The server has one ssh key and one https token. Each is read on stdin, never
from an argument, and the launcher prints only fingerprints and hosts:

```sh
memini-server git ssh-key --host git.example.org < ~/.ssh/memini_deploy_key
memini-server git https-token --host git.example.org --username oauth2 < ~/memini-git.token
memini-server git status
memini-server git remove ssh
```

For `ssh-key`, use a key without a passphrase, best a deploy key with read access made for
Memini. The launcher fetches the host keys of each `--host` with `ssh-keyscan`; compare the
fingerprints it prints with the ones your git server publishes. The key, the host keys and the
token go under `secrets/` in the state directory, for their owner only, as the compose secrets of
`.env`. The token's hosts go into `.env.memini`, and the service is recreated. Both need the
OpenSSH client (`ssh-keygen`, `ssh-keyscan`) on this computer. On Linux the secret files must also be
readable by uid 10001 (`sudo chown 10001 FILE`). The launcher does not change their owner.

## 7. Push a chat thread

Messages, mail and meeting notes come in through the push API: a push source takes the records
that a client or an agent sends it. Add one, and make a token that may only push into this
workspace (scope `ingest`), kept in a file only you can read. Then push the example thread with it:

```sh
memini source add demo chat --kind push
(set -C; umask 077; memini token create --scope ingest --workspace demo --name push > ~/.config/memini/ingest.token)
MEMINI_TOKEN_FILE=~/.config/memini/ingest.token memini push --workspace demo --source chat - < "$MEMINI_RESOURCES/docs/examples/thread.json"
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
(set -C; umask 077; printf 'Authorization: Bearer %s\n' "$(cat ~/.config/memini/ingest.token)" > ~/.config/memini/ingest.header)
curl -sS -X POST -H @"$HOME/.config/memini/ingest.header" -H 'Content-Type: application/json' \
  --data-binary @"$MEMINI_RESOURCES/docs/examples/thread.json" \
  http://127.0.0.1:6364/v1/workspaces/demo/sources/chat/records
```

Its answer counts the thread (`"unchanged":1` after the push above), and
`"index":{"indexed":true,…}` says its chunks are embedded. Search everything:

```sh
memini search "when does the nightly export run" --limit 3
```

The first hit is the thread, `chat:platform/1793610720.000100#msg-…`, with its link and messages.

## 8. Connect Claude Code

Use the [MCP setup guide](mcp/README.md) to connect Claude Code, Codex, OpenCode, Pi or an editor. It shows how to issue a workspace-scoped read token and check the connection. Once connected, ask your agent to search for the nightly export decision from the example thread.

## 9. Status

```sh
memini status
```

The first lines say whether the base is current, the database's schema version and the index: `ok`,
built with `qwen3-embedding:0.6b`. Below, one line per stream: the repository's branch and the
chat channel, with the last result, the lag and whether each is current. `memini status --gate`
exits 0 when everything is current and 6 when not, for scripts and health checks.

## Day to day

`memini-server status` lists the Compose services. `memini-server stop` stops them and keeps their volumes. `memini-server start` starts the same installation again. Keep its state directory in place. If you use `--state-dir PATH`, supply it **before** the command on every invocation, including `cli`.

| Platform | Default state directory |
|---|---|
| macOS | `~/Library/Application Support/Memini/Server` |
| Linux | `$XDG_DATA_HOME/memini/server`, or `~/.local/share/memini/server` |
| Windows | `%LOCALAPPDATA%\Memini\Server` |

State holds the managed settings and generated database password. Docker volumes hold the database, mirrors and model. `memini-server path` prints the packaged **resources**, including sample records. [Backup and restore](operations/backup.md)

### Remove the server

```sh
memini-server uninstall --yes
```

This deletes the managed server with everything in it: its containers, its volumes (the database with every workspace, source, record and token, the repository mirrors and the search model) and its state directory with the settings and the database password. It cannot be undone; take a [backup](operations/backup.md) first if you may want the data again. Docker keeps the downloaded images, so a later `memini-server start` installs a new, empty server without downloading the application again; `docker image rm` removes them. Without `--yes` the command refuses. It removes only a directory the launcher created: one without its marker file is left alone. To remove the launcher itself, uninstall its package (`brew uninstall memini-server`, `scoop uninstall memini-server`).

### Switch the MCP endpoint off

```sh
memini-server mcp off
memini-server mcp status
memini-server mcp on
```

The switch is the setting [`mcp_enabled`](configuration.md#mcp-endpoint). The launcher keeps it as `MEMINI_MCP_ENABLED` in `.env.memini` in the state directory and restarts a running server, which takes a few seconds; a stopped server takes it at its next start. While MCP is off, `/mcp` answers 404 `mcp_disabled` and agents cannot connect. The HTTP API and the CLI work as before. After the restart the launcher asks the server whether MCP is on. If the server did not apply the switch, as an image from before the switch does not, the command fails and the setting is put back.

### Output for programs

`status`, `start`, `stop`, `mcp`, `bootstrap`, `uninstall`, `repos` and `git` take `--json` after the command, for a program that drives the launcher. Each then prints one JSON object on one line of stdout and nothing else there. Docker's progress and errors go to stderr. No output holds a token, a password or the content of a secret file, with one exception: the object of `bootstrap`, whose purpose is to hand over a token.

`memini-server status --json` exits 0 whenever it can report, also for a stopped server, a missing installation and a missing Docker. Its object, on several lines here:

```json
{
  "schema": 1,
  "installed": true,
  "running": true,
  "api_url": "http://127.0.0.1:6364",
  "mcp_url": "http://127.0.0.1:6364/mcp",
  "mcp_enabled": true,
  "image": "ghcr.io/npavlov/memini:0.2.0-rc.1@sha256:2989547f…",
  "directory": "/home/me/.local/share/memini/server",
  "docker": {"available": true, "error": null},
  "services": [
    {"name": "db", "state": "running", "health": "healthy"},
    {"name": "memini", "state": "running", "health": "healthy"},
    {"name": "ollama", "state": "running", "health": "healthy"}
  ]
}
```

| Field | Value |
|---|---|
| `schema` | `1`. A field that is renamed, removed or changes its type raises it; a later version may add fields. |
| `installed` | Whether the state directory holds a managed server. Without one, `api_url`, `mcp_url`, `mcp_enabled`, `image` and `directory` are `null`. |
| `running` | Whether the container of the Memini service runs. It may still be starting: see its `health`. |
| `api_url` | The address of the HTTP API on this computer. For a running server, the port Docker published. For a stopped one, the port its next start publishes, `MEMINI_HTTP_PORT` in the state directory's `.env`; `null` if that is 0, a port Docker chooses at every start. |
| `mcp_url` | `api_url` with `/mcp`. `null` while `api_url` is `null` or MCP is not switched on. |
| `mcp_enabled` | The MCP switch as the settings have it: what `mcp on` and `mcp off` set. `null` if its value is neither true nor false. |
| `image` | The image of the service's container; before the first start, the image a start runs. |
| `directory` | The state directory. |
| `docker` | `available`: the `docker` command was found and the engine answered. If not, `error` says why in one line, and `running` and `services` read `false` and empty because they are not known. |
| `services` | The containers of the server, by name. `state` is Docker's word: `running`, `exited`, `restarting` and others. `health` is `starting`, `healthy` or `unhealthy`, and `null` for a container that reports none. Empty before the first start. |

`start`, `stop`, `mcp on` and `mcp off` print `{"schema": 1, "ok": true, "error": null}` and exit 0. On a failure `ok` is `false`, `error` is one line, and the exit status is not 0: Docker's own where Docker failed, otherwise 1. `mcp status --json` prints the status object. A wrong argument exits 2 with the usage on stderr, and a state directory that cannot be read exits 1 with a message there; stdout stays empty in both cases.

`memini-server bootstrap` creates the first admin token of the managed server, as `memini-server cli bootstrap` does, and prints it alone on stdout. With `--json` it prints `{"schema": 1, "ok": true, "error": null, "token": "memini_…"}`: the one object that holds a secret, meant for a program that reads it from the pipe and stores it. When the service refuses, because an active admin token exists or the server is not running, `ok` is `false`, `token` is `null`, `error` is the service's reason and the exit status is 1. `--force` creates another admin token although one exists.

`repos list|add|remove --json` prints `ok` and `error`, then `repositories`, every folder given to
the server, as below. `repository` is the folder just added, or the one already listed for that
path, and `null` otherwise. `warning` is one line, for example when the server is stopped and
reads the folder at its next start. On a failure `ok` is `false`, `repositories` is missing, and
the exit status is 1.

```json
{"schema": 1, "ok": true, "error": null, "repository": null, "warning": null,
 "repositories": [{"name": "harbour", "path": "/Users/sam/src/harbour",
   "location": "/srv/local/harbour", "branch": "main", "present": true}]}
```

`location` is what a git source names as its location. `branch` is the one checked out in the
folder, `null` for a detached HEAD. `present` is `false` once the folder is gone.

`git status|ssh-key|https-token|remove --json` prints `ok` and `error`, then the server's git
access, which holds no secret:

```json
{"schema": 1, "ok": true, "error": null,
 "ssh": {"key": {"type": "ED25519", "fingerprint": "SHA256:…"},
         "hosts": [{"host": "git.example.org", "keys": [{"type": "ED25519", "fingerprint": "SHA256:…"}]}]},
 "https": {"hosts": ["git.example.org"], "username": "x-access-token"}}
```

`ssh` and `https` are `null` while that access is not set.

## Machine size

See [machine requirements and measured performance](operations/performance.md#the-smallest-supported-machine).

## Troubleshooting

| What you see | What to check |
|---|---|
| Docker cannot connect | Start the engine, run `docker info`, and check that Docker Desktop uses Linux containers on Windows. |
| Port already allocated | For an existing managed installation, edit only `MEMINI_HTTP_PORT` in its state directory's `.env`, then repeat `memini-server start`. |
| An existing folder is refused | Choose a new, unused directory with `--state-dir`, or use the original managed state directory. |
| Model download interrupted | Repeat `memini-server start`. The launcher checks whether the model has already been pulled. |
| No token in a new shell | Restore `MEMINI_TOKEN_FILE` to the saved file and repeat the step 4 shell shortcut. |
| Search reports `embeddings_not_configured` | Check the server started with the release Compose settings, then run `memini-server cli reindex --workspace demo` for earlier records. |

For token recovery, private repositories and agent setup, see the [operations runbook](operations/runbook.md) and [configuration reference](configuration.md).

<a id="install-a-published-image-with-docker-only"></a>
<a id="1-download-the-release"></a>
<a id="2-set-the-database-password"></a>

## Manual Docker setup from a release ZIP

Use this Bash and OpenSSL path if you prefer to manage Compose yourself. On Windows this fallback uses a WSL shell with Docker integration.

[Download the server setup files (.zip)](https://github.com/npavlov/memini/releases/download/v0.1.0/memini-v0.1.0-server.zip). Extract the ZIP, then open a terminal in the extracted folder containing `compose.yaml`. Keep this folder for future starts. It provides the Docker settings, the CLI shortcut and sample data. Docker downloads the prebuilt application.

### Set the database password

Run this once in the extracted folder. It creates a private settings file for this installation. If you already have a `.env`, keep your password and update the two release settings in that file.

```sh
( set -C; umask 077
  password=$(openssl rand -hex 16) && printf '%s\n' \
    'COMPOSE_FILE=compose.yaml:compose.release.yaml' \
    'MEMINI_RELEASE_IMAGE=ghcr.io/npavlov/memini:0.1.0' \
    "MEMINI_DB_PASSWORD=$password" > .env )
```

### Start the Compose services

```sh
docker compose up -d --wait ollama
docker compose exec -T ollama ollama pull qwen3-embedding:0.6b
docker compose up -d --no-build --wait
docker compose ps --format '{{.Service}}  {{.Health}}'
```

Wait for `db`, `memini` and `ollama` to show `healthy`. Memini listens on this computer at `http://127.0.0.1:6364`.

### Run the CLI from the extracted folder

```sh
chmod +x bin/memini
export PATH="$PWD/bin:$PATH"
memini --version
```

The shortcut runs the CLI inside Docker. Repeat the `export` from this folder when you open a new terminal.

For the remaining examples, set `MEMINI_RESOURCES="$PWD"` in this terminal and continue at [the first admin token](#5-the-first-admin-token) using the extracted `bin/memini` shortcut. Manage this stack with `docker compose ps`, `docker compose stop` and `docker compose up -d --no-build --wait` from this folder.
