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
```

`uv sync` installs the dependencies into `.venv`. `make db` starts Postgres with pgvector on
`127.0.0.1:55432` and `make db-down` stops it; the data stays in a named volume.

## Test

| Command | What it does |
|---|---|
| `make lint` | `ruff check` and `ruff format --check` |
| `make format` | `ruff format` and `ruff check --fix` |
| `make typecheck` | `pyright` |
| `make test` | `pytest` |
| `make check` | lint, typecheck and test together |

`make check` must pass before you open a pull request.

## Project rules

- **English** for code, comments, docs and commit messages.
- **Python 3.12+, managed with uv.** Add or remove dependencies with `uv add` and `uv remove`, so that
  `pyproject.toml` and `uv.lock` change together.
- **Shell scripts run on bash 3.2**, the macOS default. No associative arrays, no `mapfile`, no `${var,,}`.
- **Fixtures are synthetic.** Write them by hand. Never commit real messages, meeting notes, transcripts,
  names of real people or companies, tokens or keys, and do not derive fixtures from real data.
- **Scope lives in the requirements.** A change of scope or of a principle goes into
  [docs/requirements.md](docs/requirements.md) in the same pull request as the code.

## Propose a change

1. Fork the repository and create a branch from `main`.
2. Keep one topic per pull request.
3. Run `make check`.
4. Add a line to [CHANGELOG.md](CHANGELOG.md) if the change is visible to users (see below).
5. Write commit messages in English, with a short subject in the imperative: "Add ...", "Fix ...".
6. Open a pull request against `main`. Say what changes and why, and link the issue.

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

- New entries go under `## [Unreleased]`, at the top.
- Group them under `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed` or
  `### Security`.
- One line per change, written for a user of Memini, not as a commit log.
- At a release, `[Unreleased]` becomes the version and date, and a new empty `[Unreleased]` starts above it.

## License

Memini is licensed under [Apache-2.0](LICENSE). Under section 5 of the license, a contribution you submit
for inclusion is licensed under the same terms, unless you state otherwise. No separate agreement is
needed.

## Code of conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
