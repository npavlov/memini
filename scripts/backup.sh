#!/usr/bin/env bash
#
# Back up the database of the compose stack: one pg_dump archive in the custom format.
#
#   scripts/backup.sh [FILE]      # default: memini-<UTC time>.dump in the current directory
#
# The archive holds the whole database `memini`: raw records, the index (chunks, vectors, symbols,
# the HNSW index of each embedding model), the knowledge journal with its triggers, sources and
# their streams and pins, runs, jobs, token digests, credentials as ciphertext, and the record of
# applied migrations. It does not hold the repository mirrors (the next sync of each git source
# clones its repository again), the credential key (MEMINI_CREDENTIAL_KEY: back it up apart from
# the archive), or the settings in .env and the environment. docs/operations/backup.md has the
# whole procedure and restoring with scripts/restore.sh.
#
# The service keeps running: pg_dump reads one consistent snapshot of the database. The archive is
# written next to FILE with mode 600 (it holds private messages and code), checked with
# `pg_restore --list`, and only then renamed to FILE; a failed backup leaves nothing under that
# name. An existing FILE is never overwritten.
#
# Another compose project (another stack beside the default one) is chosen as for docker compose:
#
#   COMPOSE_PROJECT_NAME=memini-staging scripts/backup.sh /srv/backups/staging.dump
#
# Needs docker with compose v2, and the database service running. Runs on bash 3.2 (macOS) and
# later, and on Linux with GNU tools.

set -euo pipefail

# POSTGRES_USER and POSTGRES_DB of the db service in compose.yaml.
readonly DB_USER=memini
readonly DB_NAME=memini

usage() {
  echo "usage: $0 [FILE]"
  echo "Write a pg_dump archive (custom format) of the compose stack's database to FILE"
  echo "(default: memini-<UTC time>.dump in the current directory)."
}

file=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "$0: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ -n "$file" ]; then
        echo "$0: one file only" >&2
        usage >&2
        exit 2
      fi
      file="$1"
      ;;
  esac
  shift
done

fail() {
  printf 'FAIL  %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is not installed"

[ -n "$file" ] || file="memini-$(date -u +%Y%m%dT%H%M%SZ).dump"
# Relative to where the script was called from, not to the repository.
case "$file" in
  /*) ;;
  *) file="$PWD/$file" ;;
esac
[ -e "$file" ] && fail "$file exists; choose another name"
[ -d "$(dirname "$file")" ] || fail "no directory $(dirname "$file")"
partial="$file.partial"

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

db_exec() { docker compose exec -T db "$@"; }

running="$(docker compose ps --status running --services 2>/dev/null || true)"
printf '%s\n' "$running" | grep -qx db \
  || fail "the database service is not running; start it with: docker compose up -d --wait db"
container="$(docker inspect --format '{{.Name}}' "$(docker compose ps --quiet db)")"

cleanup() {
  local status=$?
  rm -f "$partial"
  exit "$status"
}
trap cleanup EXIT

umask 077
printf 'dumping database %s in container %s\n' "$DB_NAME" "${container#/}"
db_exec pg_dump --format=custom --username="$DB_USER" --dbname="$DB_NAME" >"$partial" \
  || fail "pg_dump failed; nothing was written to $file"

# Complete and readable: pg_restore lists every entry of the archive's table of contents.
toc="$(db_exec pg_restore --list <"$partial")" \
  || fail "the archive cannot be read back; nothing was written to $file"
tables="$(printf '%s\n' "$toc" | grep -c ' TABLE DATA ' || true)"
indexes="$(printf '%s\n' "$toc" | grep -c ' INDEX ' || true)"
triggers="$(printf '%s\n' "$toc" | grep -c ' TRIGGER ' || true)"

mv "$partial" "$file"
size="$(wc -c <"$file" | tr -d ' ')"
printf 'ok    wrote %s (%s bytes, mode 600): %s tables with data, %s indexes, %s triggers\n' \
  "$file" "$size" "$tables" "$indexes" "$triggers"
printf 'note  not in the archive: the credential key (MEMINI_CREDENTIAL_KEY), .env, the mirrors\n'
