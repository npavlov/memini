# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Requirements, implementation plan and prior art documents.
- Apache-2.0 license.
- Community files: contributing guide, security policy, code of conduct and this changelog.
- Python project skeleton managed with uv: `make lint`, `make typecheck`, `make test` and `make check`.
- Development database in `compose.yaml`: Postgres 18 with pgvector 0.8.7 and pg_trgm, on
  `127.0.0.1:55432`.

[Unreleased]: https://github.com/npavlov/memini/commits/main
