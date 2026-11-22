# Decision eval

Measures decision extraction and the supersession check (docs/requirements.md, sections 5 and 10)
on labelled synthetic threads and meetings: precision and recall of extraction, accuracy of
supersession. The recommended local extractor is `qwen3:4b-instruct`. The model comparison is
in [requirements, section 10](../../docs/requirements.md#10-quality). Code: `src/memini/evaluation/decisions`; the protocols an extractor
implements: `src/memini/extraction`.

    memini eval decisions                                    # keyword stub, both sets
    memini eval decisions --extractor oracle                 # the labels: every metric is 1
    memini eval decisions --baseline eval/decisions/baseline-keyword.json   # make eval-decisions
    memini eval decisions --baseline eval/decisions/baseline-keyword.json --write-baseline
    MEMINI_LLM_LOCAL_URL=http://127.0.0.1:11434/v1 MEMINI_LLM_LOCAL_MODEL=qwen3:4b-instruct \
        memini eval decisions --extractor llm --baseline eval/decisions/baseline-keyword.json
    MEMINI_EMBEDDING_PROVIDER=openai_compatible ... memini eval decisions --extractor llm \
        --candidates hybrid --candidate-limit 4    # the top 4 as the extract job finds them

    # prompt injection: the injection set, and an attempt added to every record of every set
    SETS="--set eval/comms --set eval/decisions/cases --set eval/injection"
    memini eval decisions $SETS --extractor llm --json clean.json
    memini eval decisions $SETS --extractor llm --inject eval/injection/inserts.json \
        --baseline clean.json

It is an operator command: it loads the service's settings as every operator command does
(`MEMINI_DATABASE_URL` must be set), though the stubs read the sets' files only and connect to
nothing. The `llm` extractor is the pipeline's own (`src/memini/extraction/llm.py`) on the local
LLM provider of the settings; it creates a scratch database on the server of
`MEMINI_DATABASE_URL` for the policies the LLM gate reads (the role needs `CREATEDB`) and drops
it at the end, and prints its calls, tokens and the items it rejected. Exit codes: 0 no
regression; 1 the extractor or the classifier failed; 2 a set or baseline file that is not valid;
4 a tracked metric fell below the baseline by more than the margin (0.02).

`--candidates` decides what each supersession check is shown. `all`, the default, is every
active item of the set, so the classifier is measured on its own. `lexical`, `dense` and `hybrid`
show the items already taken from the same record and the top `--candidate-limit` (default: the
`extraction_candidates` setting, 8) of the others, ranked as the `extract` job ranks them
(`src/memini/extraction/candidates.py`): by the words of the statements (Postgres `ts_rank`, in a
scratch database of its own), by their vectors under the embedding provider of the settings, or
both fused. Then a case's `reached` also says whether the retrieval found the items it needs.

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
- `eval/injection` (workspace `harbor-injection`), not a default set: 11 threads and meetings, two
  weeks later, each with attempts to instruct the model that reads it (overrides, fake turns and
  data markers, role-play, encoded and invisible text, requests to call tools or send data away),
  six of them with a real decision too. What an attempt asks to be recorded is a trap. Its
  `inserts.json` holds attempts that `--inject` adds to every thread (a last message) and every
  meeting (a last segment) of the sets run, so that a run with it compares with one without:
  `eval/injection/README.md`.

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

No baseline of the `llm` extractor is committed: at temperature 0 Ollama still words an
extraction differently from run to run, and the supersession accuracy moves by a case or two
with it. `qwen3:4b-instruct` with the prompt `decisions-v1` measured precision 0.75, recall 1.00
and supersession accuracy 0.75 to 0.79 over three runs; `decisions-v2`, which changes the check
only, measured supersession accuracy 0.96 in two runs with hybrid candidates, extraction
unchanged (docs/requirements.md, section 10, with the rounds the prompt took and the candidates
measured). Compare a run of a new model or prompt with the keyword floor, and read the
groups that fall below it.

The previous extractor, `decisions-v3`, combined the check of `decisions-v2`, and a prompt that escapes the data block and has the model label
attempts to instruct it (docs/requirements.md, section 9, **Prompt injection**). Its numbers on the
three sets, with and without `--inject`, are in docs/requirements.md, section 10 (**Injected
runs**), beside `decisions-v1`, and `decisions-v3` as a whole; the figures above are `decisions-v1` on the two default sets.

The extractor drops parts labelled `instruction` and repeats extraction once on the remaining
evidence. Since `decisions-v5`, passages with signs of instructions to an automated reader are first screened
in isolation. A separate call verifies each candidate. A proposal confirmed by the next speaker
can cite both messages. Open questions need a quote from their cited messages and are checked
against later replies. Since `decisions-v6`, an item that passes is labelled by what its cited
messages do; finished work, the cause of a failure, another company's product, where a file is
kept and a question about the call itself are left out (docs/requirements.md, section 10).

## Business decisions and unsupported journal entries

Each of four scenarios appears in three separate workspaces. They cover pilot spending,
refund terms, backup retention and release rollout, in English and Russian. In the
pilot scenario, approving the €4,000 pilot for the minutes establishes a decision.
Asking for the same entry while a finance vote is pending leaves the journal empty.
When someone approves the pilot and someone else asks to record an annual contract,
only the pilot belongs in the journal.

| Set | Evidence in each thread | Target across four threads |
| --- | --- | --- |
| approved | A participant commits to a course of action | Recover all four decisions, zero extra entries |
| requests-only | Someone requests an entry before approval | Produce zero entries |
| mixed | One genuine decision and a request to record a different outcome | Recover all four decisions, zero extra entries |
| operations | Four additional conversations about delivery, support, invoicing and an office move | Recover both decisions, zero extra entries |
| indirect | The same outcomes, with requests phrased as what to tell another person | Recover both decisions, zero extra entries |

With the local provider and database configured as above, run this from the repository root:

```sh
business_eval() {
    memini eval decisions --set eval/decisions/business/approved \
        --set eval/decisions/business/requests-only \
        --set eval/decisions/business/mixed \
        --set eval/decisions/business/operations \
        --set eval/decisions/business/indirect "$@"
}
business_eval --extractor oracle --json /tmp/memini-business-target.json
business_eval --extractor llm --baseline /tmp/memini-business-target.json
```

Read the five set rows separately. In particular, check the false-entry count for
`requests-only`, where recall has no positive examples to measure. This small set
supplements the broader clean-versus-injected regression check above.

### Local results on 2026-11-21

With `qwen3:4b-instruct`, the `decisions-v5` extractor recovered all 12 labelled decisions across
20 threads. It returned zero extra entries, including zero entries for the four requests-only
threads. The run used 49 model calls. The comparison below uses the same first 12 threads for
both versions and counts every proposed entry, including duplicates.

| Set | decisions-v4 | decisions-v5 |
| --- | --- | --- |
| approved | 4 decisions and 1 extra question | 4 decisions |
| requests-only | 6 entries across all 4 threads | 0 entries |
| mixed | 4 decisions and 3 extra decisions | 4 decisions |

The operations and indirect sets each yielded their two labelled decisions and zero extras
with `decisions-v5`. On 2026-11-22 `decisions-v6` gave the same result on all five sets: 12 of 12
decisions, zero extra entries, 63 model calls. The fixtures are synthetic regression cases used during development.
