# S7. Who changed what?

| Area | Who | Status | Issue |
|---|---|---|---|
| Code | Team lead, engineer | ready | memini-qpk.4 |

## Who and pain

To understand a piece of code, a person needs its recent history and authors without cloning the
repository.

## Question

"Who changed the fee calculation in the last three months?"

## Answer

`list_commits` with `repository`, `paths`, or `project`, `domain`, `service` (expanded to their
paths), `author`, `since`, `until` and text in the message. Each commit: sha, author and the person
they resolve to, time, subject, body, changed paths.

## Data

Nothing new is stored. The answer is read from the bare mirror the service already keeps for S1.
Commits are not copied into the database.

## Check

On a synthetic repository: commits for one file, for a service and for a subdomain that spans two
repositories, newest first; no table gains rows.

## See also

- [All scenarios](README.md)
- [S1](S01-where-is-x-implemented.md): Where and how is X implemented?
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
