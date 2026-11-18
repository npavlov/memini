#!/usr/bin/env bash
#
# The secret scan: gitleaks at a pinned version, with the rules of .gitleaks.toml.
#
#   scripts/secret-scan.sh install   fetch the pinned gitleaks into the tool cache, checked by SHA-256
#   scripts/secret-scan.sh staged    scan what the next commit adds (the pre-commit hook; make hooks)
#   scripts/secret-scan.sh history   scan every commit reachable from HEAD (CI; make secret-scan)
#   scripts/secret-scan.sh path      print the path of the pinned gitleaks; exit 1 if not installed
#
# `staged` and `history` install gitleaks first when it is missing, so the first run needs the
# network and later runs do not. The binary is kept outside the repository and shared by its
# worktrees: ${MEMINI_TOOLS_DIR:-${XDG_CACHE_HOME:-~/.cache}/memini/tools}/gitleaks-<version>/.
#
# Findings are printed redacted: rule, file, line and commit, never the secret, so a CI log does
# not leak what the scan found. `gitleaks:allow` comments are ignored; an exception goes into
# .gitleaks.toml with its reason (CONTRIBUTING.md, "Secret scan").
#
# Exit codes: 0 nothing found, 1 a finding (or an error of gitleaks), 2 this script failed: an
# unknown command, an unsupported platform, a failed download or a checksum that does not match.
#
# A new gitleaks: set VERSION and the four checksums below from gitleaks_<version>_checksums.txt of
# the release (https://github.com/gitleaks/gitleaks/releases), run `make secret-scan`, and review
# what the new default rules find.
#
# Runs on bash 3.2 (macOS).

set -euo pipefail

VERSION="8.30.1"

checksum_of() {
    case "$1" in
        darwin_arm64) echo "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5" ;;
        darwin_x64) echo "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709" ;;
        linux_arm64) echo "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080" ;;
        linux_x64) echo "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" ;;
        *) return 1 ;;
    esac
}

TMP_DIR=""

cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

die() {
    echo "secret-scan: $*" >&2
    exit 2
}

usage() {
    sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

platform() {
    local os arch
    case "$(uname -s)" in
        Darwin) os=darwin ;;
        Linux) os=linux ;;
        *) die "no pinned gitleaks for $(uname -s)" ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64) arch=x64 ;;
        arm64 | aarch64) arch=arm64 ;;
        *) die "no pinned gitleaks for $(uname -m)" ;;
    esac
    echo "${os}_${arch}"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    else
        shasum -a 256 "$1" | cut -d ' ' -f 1
    fi
}

binary() {
    echo "${MEMINI_TOOLS_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/memini/tools}/gitleaks-${VERSION}/gitleaks"
}

# Download the release archive for this platform, check its SHA-256 against the pinned one, and
# move the binary into the cache; a concurrent install of the same version does no harm.
install_gitleaks() {
    local bin dir plat expected archive url actual
    bin="$(binary)"
    dir="$(dirname "$bin")"
    plat="$(platform)"
    expected="$(checksum_of "$plat")" || die "no pinned checksum for $plat"
    archive="gitleaks_${VERSION}_${plat}.tar.gz"
    url="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${archive}"

    mkdir -p "$dir"
    TMP_DIR="$(mktemp -d "$dir/.install.XXXXXX")"
    echo "secret-scan: fetching gitleaks ${VERSION} (${plat})" >&2
    curl --fail --silent --show-error --location --retry 3 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$TMP_DIR/$archive" "$url" || die "could not download $url"
    actual="$(sha256_of "$TMP_DIR/$archive")"
    if [ "$actual" != "$expected" ]; then
        die "SHA-256 of $archive is $actual, expected $expected"
    fi
    tar -xzf "$TMP_DIR/$archive" -C "$TMP_DIR" gitleaks
    chmod 755 "$TMP_DIR/gitleaks"
    mv -f "$TMP_DIR/gitleaks" "$bin"
    rm -rf "$TMP_DIR"
    TMP_DIR=""
}

ensure_gitleaks() {
    if [ ! -x "$(binary)" ]; then
        install_gitleaks
    fi
}

scan() {
    local root
    ensure_gitleaks
    root="$(git rev-parse --show-toplevel)"
    "$(binary)" git --no-banner --verbose --redact --ignore-gitleaks-allow \
        --config "$root/.gitleaks.toml" "$@" "$root"
}

if [ "$#" -ne 1 ]; then
    usage
fi

case "$1" in
    install)
        ensure_gitleaks
        echo "gitleaks $("$(binary)" version) at $(binary)"
        ;;
    path)
        if [ ! -x "$(binary)" ]; then
            echo "secret-scan: gitleaks ${VERSION} is not installed: scripts/secret-scan.sh install" >&2
            exit 1
        fi
        binary
        ;;
    staged)
        scan --pre-commit --staged
        ;;
    history)
        scan --log-opts="--full-history HEAD"
        ;;
    *)
        usage
        ;;
esac
