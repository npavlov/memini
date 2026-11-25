#!/usr/bin/env bash
# deploy.sh is a synthetic fixture for the chunker tests.
set -euo pipefail

TARGET_DIR="/srv/greenhouse"
RETRIES=3

log() {
  printf '%s %s\n' "$(date +%H:%M:%S)" "$*"
}

# Copy the build to the target, retrying a few times.
function copy_build {
  local attempt=1
  while (( attempt <= RETRIES )); do
    if cp -r build/. "$TARGET_DIR"; then
      return 0
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

main() {
  log "deploying to $TARGET_DIR"
  copy_build
  log "done"
}

main "$@"
