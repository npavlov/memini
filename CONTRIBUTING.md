# Contributing to Memini

Memini is at the design stage. The best contributions right now are questions and corrections to the
design, and code that follows the milestones of the plan.

Read first:

1. [docs/requirements.md](docs/requirements.md): purpose, principles, data model, intake, query surface.
2. [docs/plan.md](docs/plan.md): milestones M0–M5 and what "done" means for each.
3. [docs/prior-art.md](docs/prior-art.md): comparable projects and what is borrowed from them.

## Before you start

- A typo, a broken link or a small fix: open a pull request.
- Anything larger: open an issue first. Describe the problem and the change you have in mind.
- The twelve principles in [section 2 of the requirements](docs/requirements.md#2-principles) are
  constraints, not suggestions. If a change conflicts with one, raise it in an issue before you write code.
- Work follows the milestones in [docs/plan.md](docs/plan.md), in order. A milestone is finished when its
  "Done when" check passes. If your change belongs to a later milestone, say so in the issue.
- A security problem does not go in a public issue. See [SECURITY.md](SECURITY.md).

## Set up

Prerequisites:

- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/).
- `make`.
- Docker with compose, for the development database.

```sh
git clone https://github.com/npavlov/memini.git
cd memini
uv sync
make hooks
```

`uv sync` installs the dependencies into `.venv`. `make hooks`, once per clone, points git at the hooks
this repository versions (`core.hooksPath` = `.beads/hooks`) and fetches the pinned secret scanner
([Secret scan](#secret-scan)). `make db` starts only Postgres with pgvector on
`127.0.0.1:55432` (the `db` service of `compose.yaml`) and `make db-down` stops the compose services;
the data stays in a named volume.

To run the service from source instead of in its container, stop the container first if it runs
(`docker compose stop memini`: both use port 6364), then:

```sh
make db
export MEMINI_DATABASE_URL=postgresql://memini:memini@127.0.0.1:55432/memini
uv run memini migrate
mkdir -p ~/.config/memini
(umask 077; uv run memini bootstrap > ~/.config/memini/dev-admin.token)   # the first admin token
uv run memini serve                       # http://127.0.0.1:6364, MCP at /mcp
```

`migrate`, `bootstrap` and `serve` are operator commands: they work on the database. `bootstrap`
refuses while the database has an active admin token for every workspace, for example one made for
the compose stack, which uses the same database volume; `--force` makes another. Every other command
is a client of the running service. In a second shell:

```sh
export MEMINI_TOKEN_FILE=~/.config/memini/dev-admin.token   # MEMINI_URL defaults to http://127.0.0.1:6364
uv run memini workspace create demo
TOKEN=$(uv run memini token create --scope read --name dev)   # shown once
```

`make smoke` builds the image, starts both services with compose and checks the service end to end
from the host: health, the localhost-only port, a non-root user, the first admin token from
`memini bootstrap` in the container, the CLI on the host as a client of the API, a read token, MCP
`status`, JSON logs.

## Test

| Command | What it does |
|---|---|
| `make lint` | `ruff check` and `ruff format --check` |
| `make format` | `ruff format` and `ruff check --fix` |
| `make typecheck` | `pyright` |
| `make test` | unit tests (`pytest -m "not integration"`); no database needed |
| `make test-integration` | tests marked `integration`, against Postgres (`make db` first) |
| `make check` | lint, typecheck and unit tests together |
| `make hooks` | use the versioned git hooks and fetch the secret scanner |
| `make secret-scan` | scan every commit of `HEAD` for secrets, as CI does |
| `make bench` | the performance benchmark, in a scratch database (about 25 minutes; not part of `check` or CI; [docs/operations/performance.md](docs/operations/performance.md)) |

`make check` and `make test-integration` must pass before you open a pull request. Integration tests read
the server from `MEMINI_TEST_DATABASE_URL` (default: the `make db` database), create a new empty database
per test and drop it afterwards, so the role needs the right to create databases.

## Project rules

- **English** for code, comments, docs and commit messages.
- **Python 3.12+, managed with uv.** Add or remove dependencies with `uv add` and `uv remove`, so that
  `pyproject.toml` and `uv.lock` change together.
- **Shell scripts run on bash 3.2**, the macOS default. No associative arrays, no `mapfile`, no `${var,,}`.
- **Fixtures are synthetic.** Write them by hand. Never commit real messages, meeting notes, transcripts,
  names of real people or companies, tokens or keys, and do not derive fixtures from real data.
  Fixture data lives in a set: a directory directly under `eval/` or `tests/fixtures/`, whose root
  holds a `SYNTHETIC.md` that starts with `# SYNTHETIC DATA` and says the content is invented. E-mail
  addresses and links in fixtures use reserved names only: `example.test`, `example.org`, `.invalid`
  and the like (RFC 2606). `tests/test_synthetic_fixtures.py` checks both.
- **Scope lives in the requirements.** A change of scope or of a principle goes into
  [docs/requirements.md](docs/requirements.md) in the same pull request as the code.

## Secret scan

[gitleaks](https://github.com/gitleaks/gitleaks), at the version and SHA-256 checksums pinned in
`scripts/secret-scan.sh`, scans for keys and tokens with its default rules plus those of
`.gitleaks.toml`:

- **Before each commit**, after `make hooks`: the `pre-commit` hook in `.beads/hooks` scans what the
  commit adds and blocks it on a finding. The beads hooks in the same files run only where `bd` is
  installed; `bd hooks install` keeps the scan, which sits outside the beads markers.
- **In CI**, the `Secret scan` job scans every commit reachable from the checked-out ref, so a key that
  a later commit deletes still fails the run, and proves in a throwaway repository that the hook
  blocks a fake key (`tests/test_secret_scan.py`). `make secret-scan` runs the same scan locally.

The first scan downloads gitleaks into `~/.cache/memini/tools` (`MEMINI_TOOLS_DIR` moves it) and checks
its checksum; later scans run offline. Findings print the rule, file and line, never the secret.

On a finding, remove the secret from the commit and rotate it if it is real. A key a test needs is
fake and assembled at run time (`tests/redaction_helpers.py`), so that no literal of it is committed.
A value that only looks like a key, such as a digest or an id, goes into the allowlist of
`.gitleaks.toml`: by its shape, for the one rule it trips, with the reason. Not by path or commit, not
with a `gitleaks:allow` comment (the scan ignores those), not in a `.gitleaksignore`. Bypassing the hook
with `git commit --no-verify` does not get past CI.

## Propose a change

1. Fork the repository and create a branch from `main`.
2. Keep one topic per pull request.
3. Run `make check`.
4. Add a line to [CHANGELOG.md](CHANGELOG.md) if the change is visible to users (see below).
5. Write commit messages in English, with a short subject in the imperative: "Add ...", "Fix ...".
6. Open a pull request against `main`. Say what changes and why, and link the issue.

## Repository settings for maintainers

After the workflows land on `main`, enable the dependency graph, Dependabot alerts and security
updates, secret scanning with push protection, and private vulnerability reporting in the repository
security settings. Select advanced CodeQL setup for the checked-in workflow. The Security tab shows
CodeQL and the locked dependency audit under code scanning.

Import [the main branch ruleset](.github/rulesets/main.json) under **Settings → Rules → Rulesets →
New ruleset → Import a ruleset**. Review the checks against a pull request run and activate the rule.
It requires a pull request and passing checks from GitHub Actions. The single maintainer can merge
without a second reviewer.

Verify enforcement with a disposable pull request containing a deliberately failing test. Check that
GitHub blocks merging after the test fails, then close the pull request. Separately, verify the
server rejects a push containing a supported, inert test secret on a disposable branch. Record the
remote rejection without bypassing it. Run **Nightly eval** once from the Actions tab to seed its
model cache.

## Preview the user knowledge base

Edit the versioned Markdown under `docs/`. To preview the same content as a static site,
install Pandoc 3 and run from the checkout:

```sh
preview_dir=$(mktemp -d)
python3 scripts/build-user-guide.py "$preview_dir"
python3 -m http.server 8080 --bind 127.0.0.1 --directory "$preview_dir"
```

Open `http://127.0.0.1:8080/`. The output includes an index, static HTML, downloadable
JSON examples and a `.nojekyll` marker. Links to source or tests point to the checkout's
commit on GitHub. The Pages workflow builds and publishes the guide from `main`. Changes
to the Markdown reach the [hosted knowledge base](https://npavlov.github.io/memini/) after
the deployment succeeds. The local build requires no change to the service image.
[GitHub Pages publishing options](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

- New entries go under `## [Unreleased]`, at the top.
- Group them under `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed` or
  `### Security`.
- One line per change, written for a user of Memini, not as a commit log.
- At a release, `[Unreleased]` becomes the version and date, and a new empty `[Unreleased]` starts above it.

## Release workflow

Register a PyPI trusted publisher for the project `memini`, repository `npavlov/memini`,
workflow `release.yml` and GitHub environment `pypi`. Configure release approvals in that
environment. The workflow uses GitHub OIDC and the repository token for GHCR.

For `v0.1.0-rc.1`, set `project.version` to `0.1.0rc1`, refresh `uv.lock`, and add a
`## [0.1.0-rc.1] - YYYY-MM-DD` changelog section. A stable tag such as `v0.1.0` uses
`0.1.0` in both files. Run `python3 scripts/release-notes.py TAG` to preview the notes.
Pushing the tag builds the distributions, checks a wheel install, publishes
`ghcr.io/npavlov/memini:VERSION` for linux/amd64 and linux/arm64 with SBOM and provenance,
publishes both distributions to PyPI, and creates a draft GitHub release for review.

The platform workflow tests install, upgrade and removal on Windows x64, macOS arm64 and Linux x86-64. It attaches the Windows installer, WinGet manifest, Homebrew archives, generated formula, Docker server setup ZIP and SHA-256 list to the draft. After review, publish the release and copy its `memini.rb` into `Formula/memini.rb` on the default branch to update the tap. The RC1 Windows installer includes the compatibility fix recorded in `windows-build.json`.

## License

Memini is licensed under [Apache-2.0](LICENSE). Under section 5 of the license, a contribution you submit
for inclusion is licensed under the same terms, unless you state otherwise. No separate agreement is
needed.

## Code of conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
