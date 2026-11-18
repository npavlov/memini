# Memini

A self-hosted knowledge service for an engineer or a team lead. Code repositories, work messages and
meeting notes go in; an MCP server and an HTTP API come out.

It answers three kinds of questions:

- **Code**: where and how is X implemented, across many repositories.
- **Decisions**: what was decided about X, when, by whom, in which thread or meeting, and what it replaced.
- **My context**: which meetings did I attend about X, which messages were sent to me.

## Status

Design stage. Nothing runs yet.

- [Requirements](docs/requirements.md)
- [Implementation plan](docs/plan.md)
- [Prior art](docs/prior-art.md)

## Development database

Postgres 18 with pgvector, bound to `127.0.0.1:55432`:

```sh
docker compose up -d --wait db    # or: make db
```

The data stays in the `memini-db` volume across restarts. `MEMINI_DB_PORT` changes the host port.

## Contributing

Issues and pull requests are welcome. Setup, checks and project rules: [CONTRIBUTING.md](CONTRIBUTING.md).
Report a vulnerability privately, as described in [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
