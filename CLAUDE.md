# Memini: guide for agents

Memini is a self-hosted knowledge service: code, messages and meeting notes in; MCP and HTTP out.
Start with the [developer guide](docs/codebase.md) for feature paths, invariants and tests.
Follow the [repository workflow](.claude/rules/repository.md) for Beads, validation and merging into `main`.

## Read first

1. [docs/requirements.md](docs/requirements.md): purpose, principles, data model, intake, query surface.
2. [docs/plan.md](docs/plan.md): milestones M0–M5 and what "done" means for each.
3. [docs/prior-art.md](docs/prior-art.md): comparable projects and what is borrowed from them.

## How to work here

- Build milestone by milestone, in the order of the plan. A milestone is finished when its "Done when"
  check passes.
- The twelve principles in section 2 of the requirements are constraints, not suggestions. If a task
  conflicts with one, stop and raise it.
- A change of scope or of a principle goes into `docs/requirements.md` in the same change as the code.
- For retrieval and extraction changes, compare the relevant evaluation against its baseline.

## Never commit

- Real messages, meeting notes, transcripts, names of real people or companies, tokens or keys.
- Fixtures must be synthetic. Write them by hand; do not derive them from real data.
- `CLAUDE.local.md` and `.env` are local and ignored by git.

## Conventions

- Language of code, comments, docs and commit messages: English.
- Python 3.12+, managed with `uv`.
- Shell scripts must run on bash 3.2.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:1105d646 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
