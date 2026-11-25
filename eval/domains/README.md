# Domain eval

How well the `assign_domains` job puts a decision or an open question under the domains of the
catalog (requirements, section 10, **Domain eval**; the job: `memini.decisions.assign`).

- `set.json`: a synthetic harbour workspace, written by hand. Two projects (`port`, `fleet`), each
  with domains and subdomains, titles and descriptions; 26 items in English and Russian, each with
  the domains it belongs to (`project:domain`, `project:domain/subdomain`) or none. Some cite the
  message they were taken from; some have their project already, so only its domains are offered,
  as the job does when a project came by push, a default or a person.
- `baseline-keyword.json`: the report of the `keyword` stub, which picks the domains sharing the
  most word stems with the item. It needs no model and no database, and runs in CI.

```sh
uv run memini eval domains --baseline eval/domains/baseline-keyword.json
# The job's call through the LLM gate on the local provider (a scratch database on the server of
# MEMINI_DATABASE_URL holds the policies the gate reads; the role needs CREATEDB):
MEMINI_LLM_LOCAL_URL=http://localhost:11434/v1 MEMINI_LLM_LOCAL_MODEL=qwen3:4b-instruct \
  uv run memini eval domains --assigner llm --json eval/domains/report-qwen3.json
```

The report gives `accuracy` (the answer is exactly the labelled set of domains, none included),
`none_rate` against `expected_none_rate` (the items labelled none), `false_none` and
`false_domain`, accuracy by language, and every wrong answer. A comparison with a baseline fails
when accuracy falls, or the none rate moves away from the labelled one, by more than `--margin`
(0.02).

The stub's baseline (accuracy 0.85, none rate 0.23 for 0.23 labelled) shows what word overlap
gets right and where it fails: an item that shares a word with an unrelated description, an item
of two domains, and the Russian items, whose words no English description shares.

## The recommended local model

No baseline of a real model is committed yet. To record one: run the `llm` assigner above with the
recommended local model (`qwen3:4b-instruct`, the extractor's) on a machine with Ollama, check the
wrong answers, and commit the report as `baseline-qwen3.json` with
`--baseline eval/domains/baseline-qwen3.json --write-baseline`. A change of the prompt
(`PROMPT_VERSION` in `memini.decisions.assign`) is compared with that baseline before it is merged.
