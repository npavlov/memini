# Repository workflow

1. Run `bd prime`, inspect the issue with `bd show <id>`, and claim it with `bd update <id> --claim`. Use the acceptance criteria to decide what to verify. Record follow-up work in Beads.
2. Start a topic branch from current `main`. Use [the developer guide](../../docs/codebase.md) to find the feature's entry point, invariants and tests. Keep changes scoped to the issue. Use English for code, documentation and commit messages, and use invented data for fixtures.
3. Run all tests exclusively in GitHub Actions CI. Use the PR checks and job logs to verify results and diagnose failures. For retrieval or extraction changes, compare the relevant eval with its existing baseline and record the results.
4. When authorized to publish the change, push the topic branch and open a PR against `main`. Include the Beads ID, validation results and remaining limitations. Wait for CI on the current PR head, resolve failures and respect branch protection.
5. When the merge is authorized and required checks pass, merge through the PR. Verify that GitHub reports the PR as merged and the merge commit is on `main`. Record the PR in Beads. Report local completion, merge and release publication as separate states.
