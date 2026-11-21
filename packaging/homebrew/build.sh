#!/usr/bin/env bash
set -euo pipefail
exec uv run --no-config --script "$(dirname "$0")/build.py" "$@"
