# Operating a Memini installation

Run these commands from the installed checkout. Use `bin/memini` with Compose.
For HTTP client commands, set `MEMINI_TOKEN_FILE` to a token for the affected workspace.

| Symptom | Check | Action |
|---|---|---|
| Service unreachable (CLI exit 3) | `docker compose ps`, `docker compose logs --tail=100 memini db` | Check the published port and database health, then `docker compose up -d --wait`. |
| Token refused (CLI exit 5) | Check the token file, workspace and required scope | Use a valid `read` token for status and search. Forget requires `admin`. |
| Status gate exits 6 | `bin/memini status --json` | Identify the workspace with a stale index or the stream behind its expected window. |
| Stream behind or last sync failed | `bin/memini runs WORKSPACE --source SOURCE`, then `bin/memini run RUN_ID` | Fix the reported cause (for example source access), then `bin/memini sync --wait WORKSPACE SOURCE` and check status again. |
| Search reports a fingerprint mismatch | Check the model, dimension and chunker change against the intended configuration | Run the rebuild below in each affected workspace. |
| Forget reports `source_busy` | Inspect the source's current run | Retry after the run finishes. |

`bin/memini status --gate` returns zero when the caller's base is current. An ordinary
`status` prints the same information without failing on lag. The status CLI and API cases
are exercised in [test_cli_status.py](../../tests/test_cli_status.py).

## Back up and restore

Run `scripts/backup.sh /secure/path/memini.dump` to take a consistent database snapshot.
Keep the credential encryption key separately. Recover onto a fresh stack using
`scripts/restore.sh /secure/path/memini.dump`, start the service, then sync git sources
to restore mirrors. Compare search results and stream pins. The [backup procedure](backup.md)
covers settings, keys and restoring over an existing installation.
[Backup integration tests](../../tests/test_backup_db.py).

## Upgrade and rebuild

The supported upgrade direction is forward from the preceding release, with its database,
settings, credential keys and mirrors. Migrations are transactional per file. Memini checks
their checksums and refuses a schema newer than its own. For rollback, use the preceding
release with the backup taken before the upgrade.

1. Take a backup. Stop the Memini service with `docker compose stop memini` and switch the
   installation to the new release, retaining its settings and volumes.
2. For a source installation run `docker compose up -d --build --wait`. Startup applies
   pending migrations. Check `docker compose logs --tail=100 memini` on failure.
3. Run `bin/memini status --gate`. For a changed index fingerprint, run
   `bin/memini reindex --workspace NAME`. Keep the configured embedding endpoint available
   and git mirrors readable. Rebuilds use stored raw records and can be resumed by repeating
   the command. Check status again and repeat a known search.

The release workflow runs `scripts/test-upgrade.sh PREVIOUS_TAG` against disposable databases
initialized with the previous tag's migrations, then runs the current tool and
[fingerprint rebuild tests](../../tests/test_index_rebuild_db.py).

## Forget one record

Identify the workspace and record ID in a search result. With its admin token, run
`bin/memini forget WORKSPACE RECORD_ID`. This permanently removes the stored
record content and prevents its return on a later sync of the same source. Journal evidence
retains a forgotten reference. Earlier backups and the upstream source can still contain
the original data. [Forget integration tests](../../tests/test_forget_db.py).

## Rotate the credential encryption key

1. Keep the current key in secure storage for older backups. Configure the old key in
   `MEMINI_CREDENTIAL_OLD_KEYS_FILE` (comma-separated keys if more than one).
2. Generate a new 32-byte base64 key into a new protected file, set `MEMINI_CREDENTIAL_KEY_FILE`
   to its path, and ensure the container user can read both files. See the sample commands
   in [.env.example](../../.env.example).
3. Run `docker compose up -d --force-recreate --wait memini`, then
   `bin/memini credentials rotate-key`. An interrupted rotation can be repeated. If it
   reports a missing key, supply that key among the old keys and retry.
4. After successful rotation, remove the old-key file setting from the running installation
   and recreate the service. Retain the keys that decrypt backups for their retention period.

[Credential rotation tests](../../tests/test_cli_credentials_db.py).
