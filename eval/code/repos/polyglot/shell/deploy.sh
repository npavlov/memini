#!/usr/bin/env bash
# deploy.sh: copy a build to the server.
set -euo pipefail

TARGET_DIR="/srv/greenhouse"
MAX_TRIES=3

log() {
  printf '%s %s\n' "$(date +%H:%M:%S)" "$*"
}

# Copy the build to the target, trying a few times.
function copy_build {
  local try=1
  until (( try > MAX_TRIES )); do
    if cp -r build/. "$TARGET_DIR"; then
      return 0
    fi
    try=$((try + 1))
  done
  return 1
}

main() {
  log "deploying to $TARGET_DIR"
  copy_build
  log "finished"
}

main "$@"
