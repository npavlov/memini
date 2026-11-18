# Comms fixtures

Four weeks of chat, mail, meeting notes and wiki pages of an invented company, as ingest batches,
with answer keys for the M2 checks and labels for the decision eval. Everything is synthetic
([SYNTHETIC.md](SYNTHETIC.md)). Loaded in tests by `tests/comms_fixtures.py`.

## Layout

    SYNTHETIC.md            the marker: everything here is invented
    people.json             the cast: people, bots, mailing lists, and each one's aliases
    batches/<source>/NN-<stream>.json
                            one push API batch per file (POST /v1/workspaces/harbor/sources/<source>/records),
                            delivered in the order of NN within a source
    labels/decisions.json   decision eval: items, links, restatements, a trap, a manual correction
    labels/queries.json     M2 checks: meetings I attended, messages to me, retention, bots
    labels/timeline.json    where each ingest case happens, and the final state of every stream

Files under `labels/` are answers, not data: each says so in `"labels": true`.

## The story

Saltreed Harbourworks builds Tidewater (berth scheduling) and Beacon (lighthouse telemetry), the
two products of `eval/code` in the same workspace, `harbor`. Me is Mira Castellane, the
engineering lead. September 2026, in English and Russian:

- Beacon decides on CBOR packets (#beacon, 8 Sep), restated in the telemetry sync (10 Sep). The
  vendor's RX-40 receiver reads JSON only (mail, 18 to 22 Sep), so #beacon reverses it to gzipped
  JSON (23 Sep), restated in the Russian weekly sync (30 Sep).
- Tobias asks whether local-time tide tables are still needed (#tidewater, 9 Sep); the customer's
  answer by mail settles it: UTC only (17 Sep).
- The firmware review, in Russian and without Mira, delays the battery alarm (11 Sep).
- The pilot check-in with the customer leaves berth fees open (16 Sep); the customer asks Mira
  whether the north basin is in the pilot (mail, 25 Sep), also left open.
- The release planning (24 Sep) moves Tidewater 1.8 from 1 to 6 October; the note-taker's summary
  still says 1 October.

## Sources and streams

| Source | Streams | Records |
|---|---|---|
| `chat` | `tidewater`, `beacon`, `general` (channels), `dm` (direct messages) | threads; authors, recipients and mentions are chat user ids |
| `mail` | `inbox` (Mira's mailbox) | threads; `title` is the subject, authors and recipients are `Name <address>`, `recipients` holds to and cc and `cc` repeats the cc part |
| `meetings` | `notes` (a note-taker) | meetings; participants, speakers and owners are display names; segments have ids `s01`… |
| `docs` | none | wiki pages |

Windows are weeks starting on Monday 7 September 2026, each starting an hour before the previous
one ends (the overlap margin). Every stream ends checked from 7 September to 5 October.

| Case | Where |
|---|---|
| a late reply | `chat/01-tidewater` sends a thread, `chat/05-tidewater` sends it again with two replies |
| an edited message | `chat/06-beacon`, then `chat/08-beacon` with new text for the same message id |
| a deleted thread | `chat/06-beacon`, then a tombstone in `chat/08-beacon` |
| a thread that grows | `mail/02`, `mail/03`, `mail/04` (the same three messages twice) |
| an incomplete window | `mail/03-inbox` (`complete: false`), retried by `mail/04-inbox` from the same start |
| a snapshot | `meetings/04-notes` leaves out the notes of a deleted call |
| bots and automated mail | `U09HBOT` in #tidewater, `noreply@saltreed.example.test` in the inbox |
| aliases | Lev, Oksana and Gleb have Russian display names in Russian meetings; Mira too (`Мира Кастелан`) |

## Ingesting twice

The batches are a timeline: some carry an older version of a record that a later batch replaces.
Each batch sent again right after itself changes nothing. Sending the whole timeline again would
write the older versions on the way, as no real sync does; a second sync sends the source as it
stands now, which `second_sync()` in `tests/comms_fixtures.py` derives: per stream, one complete
snapshot of its checked range with the last version of every live record. It changes nothing.

## Labels

A record reference is `{"source", "record_kind", "external_id"}`, the way the decision tools name
a record; evidence adds `"anchor"`: a message id in a thread, a segment id (or `summary`,
`action-items`) in a meeting. People are keys of `people.json`.

`labels/decisions.json`:

- `items`: every decision and open question, with statement, `decided_at`, language, participants,
  evidence and the status it ends with (`active`, `superseded`, `closed`).
- `links`: `replaces` and `answers`, from the newer item to the older one.
- `restatements`: records that say an item again; the supersession check should call them
  duplicates.
- `traps`: what a careless extractor finds but is wrong (the summary's release date).
- `corrections`: the manual `record_decision` that corrects a trap; `supersedes` names a label.
- `no_items`: threads and meetings with nothing to extract.

Every live thread and meeting is labelled: it holds the evidence of an item or a restatement, or it
is in `no_items`, never both. Documents are not extracted (requirements, section 5).

`labels/queries.json`: the M2 checks. `expect` must come back, `not_expected` must not, and the
others are not judged. Me is `mira` with every alias of `people.json` attached, those marked
`needs_merge` included; cc counts as to me.

`tests/test_comms_fixtures.py` checks that the labels agree with the batches: references resolve,
anchors exist, every name is an alias of the cast, and the metadata side of each key holds.
