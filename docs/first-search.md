# Your first search

Import an invented two-message email conversation. By the end, a search for archive retention will point to the original thread. You can do this from a terminal without connecting a mailbox or an agent.

## Before you start

Complete the [installation through step 5](install.md#5-the-first-admin-token). Run the commands from the extracted release folder with the embedding model available, `bin` on your PATH and `MEMINI_TOKEN_FILE` pointing to the admin token. A **workspace** groups related sources. A **source** identifies where a record came from. Here the workspace is `demo` and the source is a mailbox named `mail-work`.

## Create the mailbox source

```sh
memini workspace create demo
memini source add demo mail-work --kind push
(umask 077; set -C; memini token create --scope ingest --workspace demo --name demo-mail > ~/.config/memini/demo-mail.token)
```

If you already created `demo` during installation, skip the first command. Check an existing mailbox source with `memini source show demo mail-work`. Reuse the ingest token file on later runs. The admin token stays selected for the final search.

## Import the conversation

Open the [sample email](examples/mail-thread.json). The subject is in `payload.title`; both messages carry their original Message-ID, author, recipients, text and timestamp. The thread's stable `external_id` makes repeat imports safe.

```sh
MEMINI_TOKEN_FILE="$HOME/.config/memini/demo-mail.token" memini push \
  --workspace demo --source mail-work --stream inbox --dry-run - < docs/examples/mail-thread.json
MEMINI_TOKEN_FILE="$HOME/.config/memini/demo-mail.token" memini push \
  --workspace demo --source mail-work --stream inbox - < docs/examples/mail-thread.json
```

The dry run validates one record. The first push reports one inserted record. Sending the same file again reports one unchanged record. If a row is rejected, the summary names the field to fix. An authentication error usually means the token lacks `ingest` or belongs to another workspace.

## Find the answer

```sh
memini search "archive bucket retention" --workspace demo --limit 3
```

![Terminal showing the matched email, its source and the original question about archive retention](assets/install-search.png)

Find the hit from `mail-work`. Dana asked for at least 90 days. Ana replied that the bucket kept exports for 30 days and she planned to increase it to 120. The thread shows a proposal; a [later meeting example](examples/meeting.json) records what actually changed.

If the push stored the message but search says `embeddings_not_configured`, configure Ollama and reindex `demo` as described in [troubleshooting](install.md#troubleshooting). To import your own mailbox, use the [Intake guide](intake.md).
