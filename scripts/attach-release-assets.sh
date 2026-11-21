#!/usr/bin/env bash
# Resume an interrupted upload without replacing files already distributed to users.
set -euo pipefail
tag=${1:?usage: attach-release-assets.sh TAG ARTIFACT_DIRECTORY}
directory=${2:?missing artifact directory}
repo=${GITHUB_REPOSITORY:?missing repository}
inventory=$(gh release view "$tag" --repo "$repo" --json assets --jq '.assets[] | [.name,.digest] | @tsv')
for asset in "$directory"/*; do
  name=$(basename "$asset")
  digest=$(shasum -a 256 "$asset" | cut -d ' ' -f 1)
  if printf '%s\n' "$inventory" | cut -f1 | grep -Fxq -- "$name"; then
    if ! printf '%s\n' "$inventory" | grep -Fxq -- "$name"$'\t'"sha256:$digest"; then
      echo "Release already contains a different $name" >&2
      exit 1
    fi
  else
    gh release upload "$tag" --repo "$repo" "$asset"
  fi
done
