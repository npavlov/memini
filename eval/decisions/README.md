# Decision eval

Measures decision extraction and the supersession check (docs/requirements.md, sections 5 and 10)
on labelled synthetic threads and meetings: precision and recall of extraction, accuracy of
supersession. Open question 5 (the smallest local model that extracts decisions well enough) is
settled by its numbers. Code: `src/memini/evaluation/decisions`; the protocols an extractor
implements: `src/memini/extraction`.

    memini eval decisions                                    # keyword stub, both sets
    memini eval decisions --extractor oracle                 # the labels: every metric is 1
    memini eval decisions --baseline eval/decisions/baseline-keyword.json   # make eval-decisions
    memini eval decisions --baseline eval/decisions/baseline-keyword.json --write-baseline

It is an operator command: it loads the service's settings as every operator command does
(`MEMINI_DATABASE_URL` must be set), though the stubs read the sets' files only and connect to
nothing. Exit codes: 0 no regression; 1 the extractor or the classifier failed; 2 a set or
baseline file that is not valid; 4 a tracked metric fell below the baseline by more than the
margin (0.02).

## Layout

    README.md               this file
    baseline-keyword.json   the committed baseline: the keyword stub on both sets
    cases/                  the hard cases, a set in the layout of eval/comms
        SYNTHETIC.md        the marker: everything there is invented
        batches/<source>/NN-<stream>.json
        labels/decisions.json

## Sets

A set is a directory with `batches/` (push API bodies, replayed as ingest would: the eval reads
each record as it stands after the last batch) and `labels/decisions.json` (items, links,
restatements, traps, no_items; the format is described in `eval/comms/README.md`). Each set is
one workspace: supersession is decided among its own items. The eval checks the labels against the
records before it runs: references resolve, anchors exist, statuses follow from the links, and
every live thread and meeting is labelled exactly one way.

- `eval/comms` (workspace `harbor`): the M2 fixtures, 26 threads and meetings. An explicit
  reversal (CBOR, then gzipped JSON), an open question answered by mail, a Russian decision,
  restatements in a later meeting and in #general, the note-taker's wrong summary (a trap), bots
  and chatter.
- `eval/decisions/cases` (workspace `harbor-cases`): 15 hard cases, a month later, with the same
  cast. Implicit reversals in English and Russian (a new value without naming the old one);
  decisions stated as a question and confirmed ("So we tag 2.3.0 from rc4 on Friday, right?" /
  "Right."; "Тогда храним логи стенда 90 дней?" / "Да, договорились"); a proposal dropped in the
  same thread (a trap); a Russian open question answered in a Russian meeting; hedged proposals that
  decide nothing ("Maybe we should…", "Может, попробуем…"); a cross-language duplicate; a
  restatement that names the old value too; questions answered at once; chatter in a meeting.

## Matching

Per record, deterministic, no model (`matching.py`). A proposed item matches a label of the record
it was extracted from (an item with evidence there, a restatement, a trap) of the same kind when
they share an anchor, or failing that when their statements are alike: the Dice coefficient of
their terms (words, NFKC and case-folded, without English and Russian function words, cut to five
letters; words with a digit whole) at least 0.5. Shared anchors rank first, then the Jaccard index
of the anchor sets, then likeness; each label takes one proposal, and a further proposal of the
same label is a duplicate: it counts once.

## Metrics

- Precision: items found / (items found + false items). A false item matches no label, or a trap.
  Duplicates and restatements count neither way.
- Recall: items found / labelled items.
- Supersession accuracy: cases reproduced / cases. The eval replays the journal: each proposed item,
  in record order, is classified against the active items extracted before it (`new` adds it,
  `duplicate` adds nothing, `replaces` and `answers` add it and retire their targets). A case is a
  labelled item (expected: its links, or `new`) or a restatement (expected: `duplicate` of its
  item), judged on the verdict for the first proposal that found it. The report adds how many cases
  extraction reached and how many of those the classifier got right.

Each metric is reported for `all`, per kind (supersession: per expected relation), per language
(an item's label; a false item's record) and per set. The JSON report holds every item, case and
proposal; a baseline is such a report, and a run fails (exit 4) when a tracked metric of a group
falls below it by more than the margin.

## Baseline

`baseline-keyword.json` is the `keyword` stub: cue words for extraction ("agreed", "let's",
"решили", "договорились"; a question mark after "should", "нужно ли"), statement likeness for
supersession. It is the floor a model must clear, and `make eval-decisions` compares with it.
Write it again when the sets or the stub change on purpose.
