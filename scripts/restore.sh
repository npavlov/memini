#!/usr/bin/env bash
#
# Restore the database of the compose stack from an archive of scripts/backup.sh.
#
#   scripts/restore.sh FILE             # into an empty database: a new machine, a new volume
#   scripts/restore.sh --force FILE     # replace the database that is there
#
# Steps:
#
# 1. Checks that FILE is a pg_dump archive in the custom format that pg_restore can read.
# 2. Starts the database service (`docker compose up -d --wait db`; a new volume gets an empty
#    database), and stops the Memini service if it runs, so that nothing writes during the restore.
# 3. Refuses a database `memini` that holds any table, index, sequence or view, unless --force.
# 4. Restores into a new database, `memini_restore`, with `pg_restore --no-owner --exit-on-error
#    --single-transaction`: all of it or nothing. The archive creates the extensions (vector,
#    pg_trgm), the tables, the rows, then the indexes (the HNSW index of each embedding model among
#    them), constraints and triggers. Triggers are created after the rows are loaded, so the
#    append-only checks of the knowledge journal do not fire on them.
# 5. Only then drops the database `memini` (empty, or the one --force replaces) and renames
#    `memini_restore` to `memini`. A failed restore leaves `memini` as it was.
# 6. Updates the planner's statistics (ANALYZE), which an archive does not carry, checks what came
#    back and prints what to do next.
#
# It does not start the Memini service: `docker compose up -d --wait` does, and finds no migration
# pending (or applies those of a newer Memini). The mirrors of git sources are not in a backup:
# each git source clones its repository again on its next sync; `memini sync` runs one now, and
# the commands for each git source are printed at the end. Credentials need the credential key of
# the old installation (MEMINI_CREDENTIAL_KEY), which is never in the archive.
#
# Another compose project is chosen as for docker compose:
#
#   COMPOSE_PROJECT_NAME=memini-staging MEMINI_DB_PORT=15432 MEMINI_HTTP_PORT=16364 \
#     scripts/restore.sh memini-20261118T120000Z.dump
#
# docs/operations/backup.md has the whole procedure. Needs docker with compose v2. Runs on bash 3.2
# (macOS) and later, and on Linux with GNU tools.

set -euo pipefail

# POSTGRES_USER and POSTGRES_DB of the db service in compose.yaml.
readonly DB_USER=memini
readonly DB_NAME=memini
readonly STAGING=memini_restore
readonly WAIT_TIMEOUT=120

usage() {
  echo "usage: $0 [--force] FILE"
  echo "Restore the compose stack's database from FILE, a pg_dump archive of scripts/backup.sh."
  echo "  --force   replace a database that is not empty (it is dropped once the restore succeeds)"
}

force=0
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
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
if [ -z "$file" ]; then
  usage >&2
  exit 2
fi

step() { printf '\n==>   %s\n' "$*"; }
ok() { printf 'ok    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*" >&2; }
fail() {
  printf 'FAIL  %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
case "$file" in
  /*) ;;
  *) file="$PWD/$file" ;;
esac
[ -f "$file" ] && [ -r "$file" ] || fail "cannot read $file"
[ "$(head -c 5 "$file")" = "PGDMP" ] || fail "$file is not a pg_dump archive in the custom format"

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

db_exec() { docker compose exec -T db "$@"; }
# The value of a query that returns one row of one column, in database $1. Notices stay quiet.
query() {
  docker compose exec -T --env PGOPTIONS="-c client_min_messages=warning" db \
    psql --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 \
    --username="$DB_USER" --dbname="$1" --command="$2"
}

step "the database service"
docker compose up -d --wait --wait-timeout "$WAIT_TIMEOUT" db \
  || fail "the database did not become healthy within ${WAIT_TIMEOUT}s"
running="$(docker compose ps --status running --services 2>/dev/null || true)"
stopped=0
if printf '%s\n' "$running" | grep -qx memini; then
  docker compose stop memini
  stopped=1
  ok "stopped the Memini service for the restore"
fi

step "the archive"
toc="$(db_exec pg_restore --list <"$file")" || fail "pg_restore cannot read $file"
entries="$(printf '%s\n' "$toc" | grep -c '^[0-9]' || true)"
ok "$entries entries, $(printf '%s\n' "$toc" | grep -c ' TABLE DATA ' || true) tables with data"

step "the target database $DB_NAME"
exists="$(query postgres "SELECT count(*) FROM pg_database WHERE datname = '$DB_NAME'")"
if [ "$exists" = "1" ]; then
  # Relations outside the system schemas: an extension created beforehand (vector by an
  # administrator) is no reason to refuse.
  objects="$(query "$DB_NAME" "SELECT count(*) FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast'")"
  if [ "$objects" != "0" ]; then
    if [ "$force" -ne 1 ]; then
      fail "the database $DB_NAME is not empty ($objects tables, indexes and other relations)." \
        "Back it up first (scripts/backup.sh), then run again with --force to replace it."
    fi
    warn "the database $DB_NAME is not empty ($objects relations): --force replaces it"
  else
    ok "empty"
  fi
else
  ok "none yet"
fi

step "restore into $STAGING"
query postgres "DROP DATABASE IF EXISTS $STAGING WITH (FORCE)" >/dev/null
query postgres "CREATE DATABASE $STAGING TEMPLATE template0" >/dev/null
if ! db_exec pg_restore --no-owner --exit-on-error --single-transaction \
  --username="$DB_USER" --dbname="$STAGING" <"$file"; then
  query postgres "DROP DATABASE IF EXISTS $STAGING WITH (FORCE)" >/dev/null || true
  fail "pg_restore failed; the database $DB_NAME is unchanged"
fi
ok "restored"

step "replace $DB_NAME"
query postgres "DROP DATABASE IF EXISTS $DB_NAME WITH (FORCE)" >/dev/null
query postgres "ALTER DATABASE $STAGING RENAME TO $DB_NAME" >/dev/null
query "$DB_NAME" "ANALYZE" >/dev/null
ok "$DB_NAME is the restored database; statistics updated"

step "check"
extensions="$(query "$DB_NAME" "SELECT string_agg(extname, ',' ORDER BY extname)
  FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")"
[ "$extensions" = "pg_trgm,vector" ] || fail "extensions: '$extensions', not pg_trgm and vector"
ok "extensions pg_trgm and vector"
migrations="$(query "$DB_NAME" "SELECT count(*) || ' migrations, the last ' ||
  coalesce(max(version)::text, 'none') FROM schema_migration")"
ok "$migrations"
# The HNSW index of each embedding key an index was built with (up to 4000 dimensions; none above).
missing="$(query "$DB_NAME" "SELECT count(*) FROM (SELECT DISTINCT embedding_key FROM index_state
  WHERE dimension <= 4000) k WHERE to_regclass('chunk_embedding_' || left(k.embedding_key, 32)) IS NULL")"
if [ "$missing" = "0" ]; then
  ok "vector indexes: $(query "$DB_NAME" "SELECT count(*) FROM pg_class
    WHERE relkind = 'i' AND relname ~ '^chunk_embedding_[0-9a-f]{32}$'")"
else
  warn "$missing embedding keys have no vector index: search scans their vectors until the next" \
    "write of the key creates it, or memini reindex --workspace NAME"
fi
disabled="$(query "$DB_NAME" "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled = 'D'")"
[ "$disabled" = "0" ] || fail "$disabled triggers are disabled"
ok "triggers: $(query "$DB_NAME" "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"), all enabled"
ok "$(query "$DB_NAME" "SELECT (SELECT count(*) FROM workspace) || ' workspaces, ' ||
  (SELECT count(*) FROM source) || ' sources, ' || (SELECT count(*) FROM record) || ' records, ' ||
  (SELECT count(*) FROM chunk) || ' chunks, ' || (SELECT count(*) FROM knowledge_item) ||
  ' knowledge items'")"

step "next"
if [ "$stopped" -eq 1 ]; then
  echo "The Memini service was stopped for the restore. Start it:"
else
  echo "Start the service:"
fi
echo "  docker compose up -d --wait"
echo "It needs the settings of the old installation: the same embedding model (or memini reindex),"
echo "and MEMINI_CREDENTIAL_KEY for the credentials of sources."
sources="$(query "$DB_NAME" "SELECT w.name || ' ' || s.name FROM source s
  JOIN workspace w ON w.id = s.workspace_id WHERE s.kind = 'git' ORDER BY w.name, s.name")"
if [ -n "$sources" ]; then
  echo "The mirrors of git sources are cloned again on their next sync. To clone them now, with"
  echo "the CLI and an admin token (MEMINI_URL, MEMINI_TOKEN_FILE):"
  printf '%s\n' "$sources" | while read -r workspace name; do
    echo "  memini sync --wait $workspace $name"
  done
fi
