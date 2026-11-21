#!/usr/bin/env bash
# Exercise the current tools on scratch databases initialized with the previous release schema.
set -euo pipefail
ref=${1:?usage: scripts/test-upgrade.sh PREVIOUS_TAG}
commit=$(git rev-parse --verify --end-of-options "$ref^{commit}")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
path=src/memini/db/migrations
git archive "$commit" "$path" | tar -x -C "$tmp"
export MEMINI_TEST_PREVIOUS_MIGRATIONS="$tmp/$path"
uv run --locked pytest -q tests/test_mcp_db.py tests/test_comms_db.py tests/test_query_db.py \
    tests/test_decisions_db.py tests/test_index_rebuild_db.py
