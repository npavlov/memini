#!/usr/bin/env bash
# Rehearse the published-image quickstart in a fresh Compose project with disposable volumes.
set -euo pipefail
image=${1:?usage: scripts/test-release-install.sh GHCR_IMAGE [GIT_REF]}
case "$image" in
  ghcr.io/*:*) default_ref="refs/tags/v${image##*:}" ;;
  *) default_ref=refs/heads/main ;;
esac
ref=${2:-$default_ref}
while IFS= read -r variable; do
  unset "$variable"
done < <(compgen -A variable MEMINI_)
unset COMPOSE_ENV_FILES COMPOSE_PROFILES
root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
project=memini-quickstart-$(basename "$scratch" | tr '[:upper:].' '[:lower:]-')
export COMPOSE_PROJECT_NAME="$project" COMPOSE_FILE=compose.yaml:compose.release.yaml
export MEMINI_RELEASE_IMAGE="$image" MEMINI_DB_PORT=0 MEMINI_HTTP_PORT=0
umask 077
cd "$scratch"
mkdir bin secrets
cp -f "$root/compose.yaml" "$root/compose.release.yaml" .
cp -f "$root/bin/memini" bin/
touch secrets/unset
cleanup() {
  docker compose down --volumes --remove-orphans
  rm -rf "$scratch"
}
trap cleanup EXIT

docker compose up -d --wait ollama
docker compose exec -T ollama ollama pull qwen3-embedding:0.6b
docker compose up -d --no-build --wait
bin/memini bootstrap > admin.token
export MEMINI_TOKEN_FILE="$scratch/admin.token"
bin/memini workspace create demo
bin/memini source add demo memini --location https://github.com/npavlov/memini.git --ref "$ref" \
    --include 'src/memini/cli/admin.py' --schedule 1h
bin/memini sync demo memini --wait --wait-timeout 45m
bin/memini search 'how is the first admin token created' --limit 3 --json > code.json
bin/memini source add demo chat --kind push
bin/memini token create --scope ingest --workspace demo --name push > ingest.token
MEMINI_TOKEN_FILE="$scratch/ingest.token" bin/memini push --workspace demo --source chat - \
    < "$root/docs/examples/thread.json"
bin/memini search 'when does the nightly export run' --limit 3 --json > chat.json
docker compose exec -T memini python -c '
import json, sys
assert any(h["path"] == "src/memini/cli/admin.py" for h in json.load(sys.stdin)["hits"])
' < code.json
docker compose exec -T memini python -c '
import json, sys
assert any(h["external_id"] == "platform/1793610720.000100" and "04:30" in h["snippet"]
           for h in json.load(sys.stdin)["hits"])
' < chat.json
