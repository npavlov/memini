# SYNTHETIC DATA

Everything in this directory is invented and written by hand for the retrieval eval of Memini.

- The repositories under `repos/` (tidewater, beacon, zhernov, polyglot), their code, docs and
  comments, and the harbour, lighthouse, bakery, orchard and kitchen they describe are fictional;
  nothing is copied from or derived from a real project. The files of polyglot are adapted from the
  chunking fixtures in `tests/fixtures/chunking`, in words the questions of the other
  repositories do not use, so that they do not compete for those answers.
- The baselines (`baseline-*.json`) are reports of `memini eval` over these repositories.
- No real person, company, message, credential, token or key.

This marker sits outside `repos/` on purpose: the suite digest covers every file of a repository.
Keep it that way: a change to this directory adds invented content only (CLAUDE.md, "Never commit").
