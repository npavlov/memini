#!/usr/bin/env bash
set -euo pipefail

version=${1:?usage: smoke.sh VERSION ARTIFACT_DIR [PREVIOUS_VERSION PREVIOUS_ARTIFACT_DIR]}
assets=${2:?missing artifact directory}
script_dir=$(cd "$(dirname "$0")" && pwd)
python=${PYTHON:-$(uv python find 3.12)}
tag_for() {
  "$python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from formula import release_tag; print(release_tag(sys.argv[2]))' "$script_dir" "$1"
}

export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1
if brew list --versions memini >/dev/null 2>&1; then
  echo "Run the Homebrew smoke test on a runner without an existing memini installation." >&2
  exit 1
fi
tap="memini-test/packaging-$$"
name="$tap/memini"
cleanup() {
  brew uninstall --force "$name" >/dev/null 2>&1 || true
  brew untap "$tap" >/dev/null 2>&1 || true
}
brew tap-new --no-git "$tap"
trap cleanup EXIT
tap_root=$(brew --repo "$tap")
mkdir -p "$tap_root/Formula"
formula="$tap_root/Formula/memini.rb"
old_version=${3:-$version}
old_assets=${4:-$assets}
"$python" "$script_dir/formula.py" "$old_version" "$(tag_for "$old_version")" "$old_assets" --local > "$formula"
brew install --build-from-source "$name"
test "$("$(brew --prefix)/bin/memini" --version)" = "memini $old_version"
# A first release has no older package; exercise Homebrew's upgrade path with a formula revision.
options=()
if [[ "$old_version" == "$version" ]]; then options=(--revision 1); fi
"$python" "$script_dir/formula.py" "$version" "$(tag_for "$version")" "$assets" --local "${options[@]}" > "$formula"
brew upgrade --build-from-source "$name"
test "$("$(brew --prefix)/bin/memini" --version)" = "memini $version"
brew test "$name"
brew uninstall "$name"
test ! -e "$(brew --prefix)/bin/memini"
