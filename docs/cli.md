# Command-line reference

Client commands call the HTTP API of the running service. Run them with `bin/memini …`, the CLI inside
the service's container, with the token from `MEMINI_TOKEN_FILE` or `MEMINI_TOKEN` on the host
([install.md](install.md)); or on the host with `uv run memini …` in the checkout, with
`MEMINI_URL` and `MEMINI_TOKEN` or `MEMINI_TOKEN_FILE`, where `--url URL` and `--token-file FILE`
override them for one run. Reading needs a token with the `read` scope, managing sources requires `admin`, and pushing records or writing decisions requires `ingest`. Read commands take `--json` for the API's answer as it is.

```
memini workspace create <name> | list
memini source add <ws> <name> --location URL_OR_PATH --ref REF [--ref ...] [--schedule DURATION]
                  [--include GLOB ...] [--exclude GLOB ...] [--no-builtin-excludes] [--visibility V]
                  [--retention-days N] [--llm-policy P]
memini source add <ws> <name> --kind push [--visibility V] [--retention-days N] [--llm-policy P]
memini source list <ws> | show <ws> <source> | remove <ws> <source>
memini source edit <ws> <source> [--name NEW] [--ref REF ...] [--schedule DURATION | --no-schedule] ...
memini streams <ws> <source>                     # lag, last run and queued sync of each ref
memini sync <ws> <source> [--stream KEY ...] [--wait]
memini runs <ws> [--source NAME ...] [--stream KEY ...] [--limit N] [--cursor CURSOR]
memini run <run-id>                              # one run with its report
memini token create --scope {read,ingest,admin,sensitive} [--scope ...] [--workspace NAME] [--name LABEL]
                    [--expires-in DURATION]      # prints the token alone, once
memini token list | revoke <id-or-prefix>
memini search <query> [--code] [--mode MODE] [--workspace NAME ...] [--source NAME ...] [--project NAME ...] [--domain NAME ...] [--limit N]
memini push --workspace NAME --source NAME [--stream KEY] [--batch-size N] [--dry-run] [--json] FILE|-
                    # records from JSON or JSON lines (ingest scope); intake.md
memini decision record <ws> --statement TEXT [--kind decision|open_question] [--occurred-at TIME]
                       --evidence RECORD_ID[#ANCHOR] [--participant PERSON_ID ...]
                       [--supersedes ITEM_ID[:replaces|answers] ...]   # needs the ingest scope
memini decision supersede <ws> <id> --supersedes ITEM_ID[:replaces|answers] [...]
memini decision close|retract <ws> <id>
memini extract <ws> [--source NAME ...] [--since TIME] [--dry-run]   # extract decisions from stored
                    records the current extractor has not read (needs the admin scope)
```

Operator commands work on the database directly, so they run inside the service's container:
`docker compose exec memini memini …`, or `bin/memini …`. They come before a token exists, or below
the API.

```
memini serve [--migrate]
memini migrate
memini bootstrap [--name LABEL] [--expires-in DURATION] [--force]   # the first admin token
memini workspace llm <name> [none|local|cloud]   # which LLM may read a workspace; cloud is an opt-in
memini reindex --workspace <name>
memini replay --workspace <name> --source <name> --recipe PATH   # after a parser fix, no source calls
memini export --workspace <name> [--include-private] [--include-sensitive]   # workspace records and decision history as JSONL
memini eval [--suite PATH] [--baseline PATH [--write-baseline]] [--fake-embeddings] ...
memini credentials set|list|delete|rotate-key ...   # credentials of sources, encrypted; secrets on stdin
```

`workspace llm` sits in the `workspace` group but is an operator command: no token, an admin one
included, may opt a workspace in to a cloud LLM, so the HTTP API has no route for it.

`bootstrap` gives the principal `MEMINI_PRINCIPAL` (default `owner`) a token with the `read` and `admin`
scopes for every workspace. It refuses while that principal has such a token that is active; with
`--force` it makes another one, for an operator who lost theirs (revoke the old one with
`memini token revoke`).

Exit codes: 0 done; 1 failed (no such thing, refused by the service, a failed run waited for); 2 wrong
usage or configuration (no token, a bad URL); 3 the service or the database cannot be reached; 4 `eval`
only: a metric fell below the baseline; 5 the service refused the token (unknown, revoked, expired, or
without the scope or workspace); 6 a status freshness gate failed; 130 interrupted. Errors are one line on stderr, with the API's error
code and request id.

Run `memini --help` for the full command list, then `memini COMMAND --help` for options and examples. The [configuration reference](configuration.md) covers environment variables, Compose settings, credentials and reindexing.
