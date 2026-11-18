#!/usr/bin/env bash
#
# The M0 check, end to end: `docker compose up` starts, an MCP client connects with a token and
# `status` answers.
#
#   scripts/compose-smoke.sh [--down]        # or: make smoke
#
# Builds the image and starts the database and the service, waits until both are healthy, gets an
# admin token with `memini bootstrap` inside the container (an operator command), then uses the CLI
# from the host as a client of the HTTP API (`uv run memini`, with MEMINI_URL and MEMINI_TOKEN_FILE)
# to create a workspace and a read token, and checks from the host:
#
# - the service port is published on 127.0.0.1 only, and the service does not run as root;
# - git and ssh are installed, the mirror volume is writable and the root filesystem is not;
# - the CLI on the host creates and lists a workspace and tokens, and no listing holds a token;
# - /healthz and /readyz answer 200;
# - /mcp refuses a request without a token, and one with an unknown token (401);
# - a real MCP client with the read token lists the tools and calls `status`, which answers with the
#   database reachable and every migration applied;
# - every line of the service log is JSON, and none holds a token.
#
# The tokens are never printed, never on a command line, and are revoked at the end; the admin token
# expires within the hour should that fail. Exits non-zero at the first failure.
#
#   --down   remove the containers afterwards (`docker compose down`, database included). The data
#            volume stays: volumes are never removed here. Without it the stack keeps running.
#
# Beside a running stack, use another compose project and other host ports; its containers and
# volumes are separate (<project>_memini-db, <project>_memini-mirrors), the image tag is shared:
#
#   COMPOSE_PROJECT_NAME=memini-smoke MEMINI_HTTP_PORT=16364 MEMINI_DB_PORT=15432 \
#     scripts/compose-smoke.sh --down
#
# Needs docker with compose v2, and uv. Runs on bash 3.2 (macOS) and later, and on Linux with GNU
# tools (CI runs it with `make smoke` on ubuntu-latest).

set -euo pipefail

readonly WORKSPACE=smoke
readonly TOKEN_NAME=compose-smoke
readonly ADMIN_TOKEN_NAME=compose-smoke-admin
readonly CONTAINER_PORT=6364
readonly WAIT_TIMEOUT=180

usage() {
  echo "usage: $0 [--down]"
  echo "Start the compose stack and check that an MCP client can call status with a token."
}

down=0
while [ $# -gt 0 ]; do
  case "$1" in
    --down) down=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "$0: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

step() { printf '\n==>   %s\n' "$*"; }
ok() { printf 'ok    %s\n' "$*"; }
fail() {
  printf 'FAIL  %s\n' "$*" >&2
  exit 1
}

for tool in docker uv; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"
done

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

# The tokens live in this directory (mode 700) and in variables, never on a command line.
tmp="$(mktemp -d "${TMPDIR:-/tmp}/memini-smoke.XXXXXX")"
admin_file="$tmp/admin.token"
service_url=""
admin_token=""
admin_id=""
token=""
token_id=""

# The CLI on the host, as a client of the service: the URL and the admin token's file, and no
# MEMINI_TOKEN from the caller's shell, which would clash with the file.
memini_cli() {
  env -u MEMINI_TOKEN MEMINI_URL="$service_url" MEMINI_TOKEN_FILE="$admin_file" \
    uv run --quiet memini "$@"
}

revoke() { # <what> <token id>
  if memini_cli token revoke "$2" >/dev/null 2>"$tmp/revoke.err"; then
    ok "revoked the $1 $2"
  else
    printf 'WARN  could not revoke the %s %s: %s\n' "$1" "$2" "$(cat "$tmp/revoke.err")" >&2
    printf 'WARN  revoke it with: memini token revoke %s\n' "$2" >&2
  fi
}

cleanup() {
  local status=$?
  set +e
  if [ -n "$token_id" ]; then
    revoke "smoke token" "$token_id"
  elif [ -n "$token" ]; then
    printf 'WARN  the smoke token id is unknown; find it with: memini token list\n' >&2
  fi
  # Last: the admin token revokes itself. It expires within the hour should this fail.
  if [ -n "$admin_id" ]; then
    revoke "admin token" "$admin_id"
  elif [ -n "$admin_token" ]; then
    printf 'WARN  the admin token id is unknown; it expires within the hour\n' >&2
  fi
  if [ "$down" -eq 1 ]; then
    step "docker compose down (the data volume stays)"
    docker compose down
  fi
  rm -rf "$tmp"
  if [ "$status" -eq 0 ]; then
    printf '\nPASS  M0: docker compose up starts, an MCP client connects with a token and status answers.\n'
  fi
  exit "$status"
}
trap cleanup EXIT

step "docker compose up -d --build --wait"
if ! docker compose up -d --build --wait --wait-timeout "$WAIT_TIMEOUT"; then
  docker compose ps --all >&2 || true
  docker compose logs --no-color --tail 40 memini >&2 || true
  fail "the image did not build, or the stack did not become healthy within ${WAIT_TIMEOUT}s"
fi

container="$(docker compose ps --quiet memini)"
[ -n "$container" ] || fail "no memini container"
health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container")"
[ "$health" = "healthy" ] || fail "the memini container is '${health:-without a healthcheck}', not healthy"
ok "memini is healthy"

step "port binding and user"
binding="$(docker compose port memini "$CONTAINER_PORT")"
case "$binding" in
  127.0.0.1:[0-9]*) ;;
  *) fail "port $CONTAINER_PORT is published on '$binding', not on 127.0.0.1" ;;
esac
host_ips="$(docker inspect --format \
  '{{range $port, $bindings := .NetworkSettings.Ports}}{{range $bindings}}{{.HostIp}} {{end}}{{end}}' \
  "$container")"
for ip in $host_ips; do
  [ "$ip" = "127.0.0.1" ] || fail "a port of memini is published on $ip, not on 127.0.0.1 only"
done
ok "published on $binding only"
service_url="http://$binding"

uid="$(docker compose exec -T memini id -u)"
[ "$uid" != "0" ] || fail "the service runs as root"
ok "runs as uid $uid, not root"

step "git, ssh and the mirror volume"
git_version="$(docker compose exec -T memini git --version)" || fail "git is not installed in the image"
docker compose exec -T memini sh -c 'command -v ssh' >/dev/null || fail "ssh is not installed in the image"
ok "$git_version, ssh"
# shellcheck disable=SC2016 # expanded inside the container
docker compose exec -T memini sh -c 'test -d "$MEMINI_GIT_MIRROR_DIR" && test -w "$MEMINI_GIT_MIRROR_DIR"' \
  || fail "the mirror directory is missing or not writable by the service user"
if docker compose exec -T memini sh -c 'touch /opt/memini/write-test' >/dev/null 2>&1; then
  fail "the root filesystem of the service is writable"
fi
ok "the mirror volume is writable; the root filesystem is not"

step "the first admin token, with memini bootstrap in the container"
# --force: on a stack that is in use, its own admin token stays where it is. The token alone goes to
# stdout, here into a file; the details, with the token id, to stderr.
if ! docker compose exec -T memini memini bootstrap --force --name "$ADMIN_TOKEN_NAME" \
  --expires-in 1h >"$admin_file" 2>"$tmp/bootstrap.err"; then
  fail "bootstrap: $(cat "$tmp/bootstrap.err")"
fi
admin_token="$(cat "$admin_file")"
admin_id="$(sed -n 's/^memini: created token \([0-9a-f-]*\) .*/\1/p' "$tmp/bootstrap.err")"
case "$admin_token" in
  memini_*) ;;
  *) fail "bootstrap printed no token" ;;
esac
case "$admin_token" in
  *[[:space:]]*) fail "bootstrap printed more than the token" ;;
esac
ok "created admin token ${admin_id:-?} (not shown; expires within the hour)"

step "workspace and read token, with the CLI on the host: $service_url"
if out="$(memini_cli workspace create "$WORKSPACE" 2>&1)"; then
  ok "$out"
else
  case "$out" in
    *"(conflict, HTTP 409"*) ok "workspace $WORKSPACE exists already" ;;
    *) fail "workspace create: $out" ;;
  esac
fi
memini_cli workspace list >"$tmp/workspaces.txt" 2>"$tmp/cli.err" \
  || fail "workspace list: $(cat "$tmp/cli.err")"
grep -q "^$WORKSPACE " "$tmp/workspaces.txt" || fail "workspace list does not show $WORKSPACE"
ok "workspace list shows $WORKSPACE"

# The token alone goes to stdout; the details, with the token id, to stderr.
if ! token="$(memini_cli token create --scope read --name "$TOKEN_NAME" 2>"$tmp/token.err")"; then
  fail "token create: $(cat "$tmp/token.err")"
fi
token_id="$(sed -n 's/^memini: created token \([0-9a-f-]*\) .*/\1/p' "$tmp/token.err")"
case "$token" in
  "" | *[[:space:]]*) fail "token create printed no token, or more than one line" ;;
esac
ok "created read token ${token_id:-?} (not shown)"

memini_cli token list >"$tmp/tokens.txt" 2>"$tmp/cli.err" || fail "token list: $(cat "$tmp/cli.err")"
for id in "$admin_id" "$token_id"; do
  [ -z "$id" ] || grep -q "^$id " "$tmp/tokens.txt" || fail "token list does not show token $id"
done
if grep -qF -e "$token" -e "$admin_token" "$tmp/tokens.txt" "$tmp/token.err" "$tmp/bootstrap.err"; then
  fail "a listing or a message holds a token"
fi
ok "token list shows both tokens, by prefix only"

step "MCP from the host: http://$binding/mcp"
MEMINI_SMOKE_TOKEN="$token" uv run --quiet python scripts/compose_smoke.py mcp "http://$binding"

step "service log"
docker compose logs --no-color --no-log-prefix memini >"$tmp/memini.log" 2>&1
MEMINI_SMOKE_TOKEN="$token" MEMINI_SMOKE_ADMIN_TOKEN="$admin_token" \
  uv run --quiet python scripts/compose_smoke.py logs "$tmp/memini.log"
