# Releasing

How a release or a release candidate of Memini is cut. A tag publishes the service; the server
launcher and the desktop app follow it in a second change, because they pin the image the tag
built. Version numbers are written in two forms: `0.2.0-rc.2` (tag, desktop app, launcher package)
and `0.2.0rc2` (Python).

## 1. Write the version

```sh
uv run python scripts/bump-version.py 0.2.0-rc.2
```

It writes the version to `pyproject.toml`, `uv.lock`, `desktop/src-tauri/Cargo.toml`,
`desktop/src-tauri/Cargo.lock` and `desktop/package.json`, and moves the entries under
`[Unreleased]` in `CHANGELOG.md` to a section of the new version dated today. It refuses when
`[Unreleased]` is empty. Open a PR with the result and merge it when its checks pass;
`tests/test_version.py` fails while the app and the service disagree.

## 2. Tag

```sh
git switch main && git pull --ff-only
git tag -a v0.2.0-rc.2 -m "Memini 0.2.0 release candidate 2"
git push origin v0.2.0-rc.2
```

The Release workflow checks the tag against the versions (`scripts/release-notes.py`: project,
desktop app and changelog section), builds and tests the distributions, publishes the image
`ghcr.io/npavlov/memini:<version>` for amd64 and arm64, builds the installers, and opens a draft
release, a pre-release for an `-rc.N` tag.

It also builds the desktop app from the tag on each platform
([desktop-app.yml](../.github/workflows/desktop-app.yml), the workflow that checks the app on a
pull request, here without the Rust cache) and attaches it, unsigned, with `desktop-SHA256SUMS`:

- `Memini-<version>-macos-arm64.dmg`, `Memini-<version>-macos-x86_64.dmg`
- `Memini-<version>-windows-x64-setup.exe`
- `Memini-<version>-linux-x86_64.AppImage`, `Memini-<version>-linux-amd64.deb`

The build fails unless the app's version is the tag's. The release notes end with how to open the
unsigned app the first time (`DESKTOP_APP` in `scripts/release-notes.py`).

The app's launcher installs this release's server. The desktop build waits for the image job and
gets the published image with its digest (`ghcr.io/npavlov/memini:<version>@sha256:…`). The
sidecar freeze (`desktop/sidecar/freeze.py --release-image`) pins that image in the copy of the
launcher it freezes, with the compose files of the tag and their checksums, and fails unless the
frozen launcher's `status --json` names that image. It accepts only an image of this repository
with the tag's version and a digest. A pull request's build keeps the pin of
`scripts/memini-server.py`.

## 3. Point the launcher at the image

The released app already installs the new image (step 2). This step moves the repository's pin,
which the Homebrew and Scoop packages and every later build of the app from `main` use.

When the Release run has passed, read the digest of the published image:

```sh
docker buildx imagetools inspect ghcr.io/npavlov/memini:0.2.0-rc.2 | grep -m1 Digest
```

In one PR:

- `IMAGE` in `scripts/memini-server.py`: the tag and that digest.
- `COMPOSE_FILES` in the same file: the SHA-256 of `compose.yaml` and `compose.release.yaml` at
  the new tag, if they changed.
- `TAG` and `VERSION` in `packaging/server/build.py`, and `REVISION = 1`.
- Rebuild the package and copy its recipes:
  `uv run --no-project --python 3.12 python packaging/server/build.py <dir>`, then
  `cp <dir>/memini-server.rb Formula/` and `cp <dir>/memini-server.json bucket/`.
- A line under `[Unreleased]` in `CHANGELOG.md`.

The app built from `main` after this commit installs the new server, as the released app does.
A managed server moves to the new image at its next start; the service migrates its database
when it starts.

## 4. Attach the launcher package

```sh
gh release upload v0.2.0-rc.2 <dir>/memini-server-0.2.0-rc.2-1.zip
```

Homebrew and Scoop download this file; their recipes on `main` point to it. A later change of the
launcher raises `REVISION` and needs its archive uploaded the same way.

## 5. Publish

Read the draft release, then publish it. Publishing is the owner's step.
