# Backup and restore

A backup of Memini is one archive of its database, made with `pg_dump` in the custom format while
the service runs. The repository mirrors are not backed up: they are copies of the repositories,
and each git source clones its repository again on its first sync after a restore. The credential
key is backed up apart from the archive. [requirements.md](../requirements.md), section 7.

- Back up: `scripts/backup.sh [FILE]`
- Restore: `scripts/restore.sh FILE`, or `scripts/restore.sh --force FILE` over a database in use
- Test: `make backup-test`; in CI, part of `make test-integration` (tests/test_backup_db.py)

Both scripts work on the compose stack of this repository, through `docker compose exec` into its
`db` service, so they need no Postgres client on the host. For another stack beside the default
one, set `COMPOSE_PROJECT_NAME` (and its ports) as for `docker compose` itself.

## What is in a backup

| | In the archive | Why |
|---|---|---|
| Raw records, streams with their cursors and pins, sources, workspaces | yes | The source of truth (principle 1) |
| Chunks, vectors, symbols, `index_state` | yes | Rebuilding them means embedding every text again |
| The HNSW index of each embedding model | yes, as its definition | Rebuilt from the vectors by the restore |
| The knowledge journal and its triggers | yes | Items cannot be extracted again the same way |
| Runs, jobs, `schema_migration` | yes | History; the service finds the schema at its version |
| Tokens | yes, as SHA-256 digests | The tokens in use keep working |
| Credentials of sources | yes, as AES-256-GCM ciphertext | Readable only with the credential key |
| The credential key (`MEMINI_CREDENTIAL_KEY`) | **no** | Back it up apart (below) |
| Repository mirrors (volume `memini-mirrors`) | no | Cloned again by the next sync |
| Settings: `.env`, `.env.memini`, the environment, a TOML settings file | no | Keep them with the installation |
| Other secret files of compose (tokens, API keys, the ssh key) | no | Keep them apart, like the key |
| The database role and its password | no | Compose creates the role on the first start |

An archive holds private messages, meeting notes and code. `scripts/backup.sh` writes it with mode
600; store it where only you can read it, encrypted if it leaves the machine, and keep it no longer
than you need: a restore brings back every record the archive holds, also one forgotten or expired
since.

## Back up

```sh
scripts/backup.sh                                   # memini-<UTC time>.dump in the current directory
scripts/backup.sh /srv/backups/memini-nightly.dump  # or a file of your choice; never overwritten
```

The script checks that the database service runs, then writes
`docker compose exec -T db pg_dump --format=custom --username=memini --dbname=memini` to
`FILE.partial`, reads the archive back with `pg_restore --list`, and only then renames it to FILE.
A backup that fails leaves no file under that name. The archive is compressed; `pg_dump` reads one
consistent snapshot, so the service keeps serving and syncing meanwhile.

For a nightly backup, run it from cron or a systemd timer, e.g.:

```cron
15 3 * * * cd /opt/memini && scripts/backup.sh /srv/backups/memini-$(date -u +\%Y\%m\%d).dump
```

### The credential key

The credentials of sources are encrypted under `MEMINI_CREDENTIAL_KEY` (or the file
`MEMINI_CREDENTIAL_KEY_FILE` names; under compose, the file on the host that `.env` names, e.g.
`secrets/credential_key`), which never enters the database. Keep a copy of the key
apart from the archives, e.g. in a password manager: whoever has both can read the credentials.
Without the key a restored database still serves everything else; each credential is then set again
with `memini credentials set`.

After `memini credentials rotate-key`, older archives hold credentials under the old key. Keep the
old key as long as you keep those archives, and restore them with it in
`MEMINI_CREDENTIAL_OLD_KEYS`, then rotate again.

## Restore on a new machine

1. Clone this repository at the version that made the archive, or a newer one. An older Memini
   refuses a database migrated by a newer one.
2. Put back the settings: `.env` (ports, `MEMINI_DB_PASSWORD`, the paths of the secret files),
   `.env.memini` (the embedding settings and the others), and the secret files, the credential key
   among them, readable by the service ([configuration.md](../configuration.md), "Docker
   compose"). With another embedding model, search is refused until `memini reindex` rebuilds
   each workspace (principle 9); with the same one, nothing needs rebuilding. Local repositories
   (`MEMINI_GIT_LOCAL_ROOTS`) are mounted at the same paths as before.
3. Restore:

   ```sh
   scripts/restore.sh memini-20261118T031500Z.dump
   ```

   It starts the database service (a new volume gets an empty database `memini`), stops the Memini
   service if it runs, refuses a database that holds any table, restores into a new database
   `memini_restore` with `pg_restore --no-owner --exit-on-error --single-transaction`, and only
   then drops `memini` and renames `memini_restore` to it. A restore that fails leaves `memini` as
   it was. It then runs `ANALYZE` (an archive carries no planner statistics), checks the
   extensions, migrations, vector indexes and triggers, and prints the next commands.
4. Start the service: `docker compose up -d --wait`. It finds no migration pending, or applies
   those of a newer Memini.
5. Bring the mirrors back: each git source clones its repository on its next sync, by its schedule
   if it has one, or now with `memini sync --wait WORKSPACE SOURCE` (the script prints one line per
   git source). Until then search answers from the database, while `read_source` of a file answers
   that the repository is not synced yet. The sync goes on from the pinned commit: only files
   changed since the backup are read and embedded.

### Restore over an installation in use

`scripts/restore.sh --force FILE` replaces a database that is not empty. Take a backup of it first.
The old database is dropped only after the new one has been restored in full, so the disk needs
room for both meanwhile.

### Without the scripts

On another Postgres (18 with pgvector), the same steps by hand:

```sh
pg_dump --format=custom --dbname=memini > memini.dump
createdb --template=template0 memini_restored
pg_restore --no-owner --exit-on-error --single-transaction --dbname=memini_restored memini.dump
psql --dbname=memini_restored --command=ANALYZE
```

The archive creates the extensions `vector` and `pg_trgm`. `vector` needs a superuser: where the
restoring role is not one, an administrator runs `CREATE EXTENSION vector` in the new database
first. The database needs a UTF-8 encoding and character type (lexical search, requirements
section 9); `template0` with the server's default locale gives that on the compose image.

## Verify

- `scripts/restore.sh` prints the extensions, the number of migrations, the vector indexes, the
  triggers and the number of workspaces, sources, records, chunks and knowledge items.
- `docker compose exec memini memini migrate` says that no migration is pending.
- `memini search "<something you know is there>"` returns hits; `memini streams WORKSPACE SOURCE`
  shows each stream's checkpoint, as it was at the backup.
- `memini sync --wait WORKSPACE SOURCE` of a git source succeeds and clones its mirror.

## What comes back as it was, and what to redo

- **Vector indexes.** The service creates the HNSW index of an embedding model when it first writes
  chunks of that model (`memini.index.vectors`), not in a migration. It is still an index of the
  table `chunk`, so `pg_dump` writes its definition, and the restore builds it again from the
  vectors, under the same name. No reindex is needed. A restore of many vectors spends most of its
  time there. Should one be missing, search compares every vector of that model until the next
  write of the model creates the index, or `memini reindex` does.
- **The journal's triggers.** `pg_dump` writes triggers after the rows, so a full restore loads the
  items, their evidence and links as they were, superseded ones included, and the triggers guard
  them from then on. A data-only restore (`pg_restore --data-only`) into an existing schema would
  fire them and needs `--disable-triggers`; the scripts never do one.
- **The index fingerprint** (`index_state`) comes with the archive: search works at once with the
  same embedding settings.
- **Migrations.** `schema_migration` comes with the archive, so the service finds the schema at
  the archive's version.
- **Tokens.** Tokens created after the backup do not exist; a token revoked after the backup is
  valid again. Revoke it again (`memini token revoke`).
- **Push sources.** Records pushed after the backup are not in it, and each stream's checkpoint is
  where it was at the backup. A client that starts its next window at the stream's checkpoint
  sends them again; one that keeps its own position must be set back to it.
- **Git sources.** Commits after the backup are synced by the next sync, from the pinned commit; a
  pinned commit that a force push removed makes that sync list the ref in full.
- **Jobs.** A job that was running when the archive was made is queued again once its lease runs
  out.

## The automated test

`tests/test_backup_db.py` fills a database through the service's own code (a git source synced with
the FakeEmbedder, a push source with a thread and a document indexed at ingest, knowledge items
with evidence, one superseding another, a credential, tokens), dumps it and restores it as the
scripts do into a new, empty database, and checks that:

- every row of every table and every schema object (extensions, generated columns, functions,
  constraints, triggers, indexes) came back, the HNSW index included, and a dense query uses it;
- no migration is pending and the index fingerprint is the service's;
- hybrid, dense, lexical, exact, regex and symbol searches return the same hits with the same
  scores;
- the journal refuses an edit or a delete, a token in use authenticates and a revoked one does not,
  and the credential decrypts with its key and not with another;
- the archive holds no secret in clear: no API key, token or key;
- a sync after the restore, with an empty mirror directory, clones the repository and syncs only
  the commit made after the backup.

`pg_dump` refuses a server of a newer major version than its own, so the test takes the programs
of the server's version: from the container `MEMINI_TEST_PG_CONTAINER` names; else from the PATH
if they are recent enough; else, with `docker exec`, from the container that publishes the test
server's port (the compose database of `make db`, the service container of CI). Run it alone with
`make backup-test`.


## Export one workspace as JSONL

Run `memini export --workspace NAME > workspace.jsonl` inside the service container, or use
`docker compose exec -T memini memini export --workspace NAME > workspace.jsonl` from its host.
The operator command writes one JSON object per line from a single database snapshot. Record rows
have `type: record`, source identity, external ID, timestamps, content hash and stored content.
Git file records identify the ref, path and blob in the repository mirror. Journal rows have
`type: knowledge_item` and include status, evidence, participants and visible supersession links.

The default includes records from sources with `workspace` visibility. A decision enters the
export only when every evidence source has that visibility. Add `--include-private` to include
private sources and decisions, including evidence whose source has since been removed.
