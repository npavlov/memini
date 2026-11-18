#!/usr/bin/env bash
#
# The M0 check, end to end: `docker compose up` starts, an MCP client connects with a token and
# `status` answers.
#
#   scripts/compose-smoke.sh [--down]        # or: make smoke
#
# Builds the image and starts the database and the service, waits until both are healthy, creates
# a workspace and a read token with the CLI inside the container, then checks from the host:
#
# - the service port is published on 127.0.0.1 only, and the service does not run as root;
# - /healthz and /readyz answer 200;
# - /mcp refuses a request without a token, and one with an unknown token (401);
# - a real MCP client with the token lists the tools and calls `status`, which answers with the
#   database reachable and every migration applied;
# - every line of the service log is JSON, and none holds the token.
#
# The token is never printed and is revoked at the end. Exits non-zero at the first failure.
#
#   --down   remove the containers afterwards (`docker compose down`, database included). The data
#            volume stays: volumes are never removed here. Without it the stack keeps running.
#
# Needs docker with compose v2, and uv. Runs on bash 3.2 (macOS) and later.

set -euo pipefail

readonly WORKSPACE=smoke
readonly TOKEN_NAME=compose-smoke
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

tmp="$(mktemp -d "${TMPDIR:-/tmp}/memini-smoke.XXXXXX")"
token=""
token_id=""

cleanup() {
  local status=$?
  set +e
  if [ -n "$token_id" ]; then
    if docker compose exec -T memini memini token revoke "$token_id" >/dev/null 2>&1; then
      ok "revoked the smoke token $token_id"
    else
      printf 'WARN  could not revoke the smoke token %s; run: docker compose exec memini memini token revoke %s\n' \
        "$token_id" "$token_id" >&2
    fi
  elif [ -n "$token" ]; then
    printf 'WARN  the smoke token id is unknown; revoke it: docker compose exec memini memini token list\n' >&2
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
  fail "the stack did not become healthy within ${WAIT_TIMEOUT}s"
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

uid="$(docker compose exec -T memini id -u)"
[ "$uid" != "0" ] || fail "the service runs as root"
ok "runs as uid $uid, not root"

step "workspace and token, with the CLI in the container"
if out="$(docker compose exec -T memini memini workspace create "$WORKSPACE" 2>&1)"; then
  ok "$out"
else
  case "$out" in
    *"named $WORKSPACE exists"*) ok "workspace $WORKSPACE exists already" ;;
    *) fail "workspace create: $out" ;;
  esac
fi

# The token alone goes to stdout; the details, with the token id, to stderr.
if ! token="$(docker compose exec -T memini memini token create --scope read --name "$TOKEN_NAME" \
  2>"$tmp/token.err")"; then
  fail "token create: $(cat "$tmp/token.err")"
fi
token_id="$(sed -n 's/^memini: created token \([0-9a-f-]*\) .*/\1/p' "$tmp/token.err")"
case "$token" in
  "" | *[[:space:]]*) fail "token create printed no token, or more than one line" ;;
esac
ok "created read token ${token_id:-?} (not shown)"

step "MCP from the host: http://$binding/mcp"
MEMINI_SMOKE_TOKEN="$token" uv run --quiet python scripts/compose_smoke.py mcp "http://$binding"

step "service log"
docker compose logs --no-color --no-log-prefix memini >"$tmp/memini.log" 2>&1
MEMINI_SMOKE_TOKEN="$token" uv run --quiet python scripts/compose_smoke.py logs "$tmp/memini.log"
