# Memini: user scenarios

Memini is built from the questions people need answered, not from what the code makes easy. A
scenario names who asks and what hurts, the question, how Memini answers it, what data must be in
the base and how it gets there, and how the answer is checked. The
[requirements](../requirements.md) describe the contracts; the [plan](../plan.md) describes the
build order.

## How scenarios are used

1. A scenario comes first: who, pain, question, answer, data, check.
2. An issue comes second and names the scenarios it serves.
3. A feature that serves no scenario does not go into a release.

Each scenario is one file with the same parts:

| Part | What it says |
|---|---|
| Who and pain | The person and the situation that hurts today. |
| Question | The question in the person's own words. |
| Answer | The tool or route, its filters, and the shape of the answer. This is where the API is designed. |
| Data | What must be in the base to answer, who puts it there and by which path. |
| Check | The eval question or acceptance test that proves the answer. |

Status is `works` (in 0.1.0), `partial`, `planned` (with its issue) or `to confirm` (the owner has
not confirmed the pain). Names of tools, filters and tables under a planned scenario are working
names: nothing planned is in the code yet.

## People

- **Engineer.** Works in several repositories, usually through a coding agent.
- **Team lead.** Follows several projects, their conversations and their decisions.
- **Freelancer.** Works for several companies, each with several projects.
- **Operator.** Installs and runs Memini. Often the same person as one of the above.

A question usually arrives through an agent over MCP. The agent is the hands; the person has the
pain.

## Scenarios by area

### Code

| Id | Question | Who | Status | Issue |
|---|---|---|---|---|
| [S1](S01-where-is-x-implemented.md) | Where and how is X implemented? | Engineer | works |  |
| [S7](S07-who-changed-what.md) | Who changed what? | Team lead, engineer | planned | memini-qpk.4 |
| [S10](S10-definitions-in-my-language.md) | Find a definition in my language | Engineer on another stack | partial | memini-qpk.2 |

### Catalog: what code and conversations belong to

| Id | Question | Who | Status | Issue |
|---|---|---|---|---|
| [S2](S02-code-for-a-domain.md) | Which code is responsible for domain X? | Engineer, team lead | planned | memini-qpk.1 |
| [S3](S03-what-code-belongs-to.md) | What does this piece of code belong to? | Engineer | planned | memini-qpk.1 |
| [S8](S08-several-companies-and-projects.md) | Several companies, several projects each | Freelancer | partial | memini-qpk.1 |
| [S9](S09-project-map-by-an-agent.md) | The project map without drawing it by hand | New user | planned | memini-qpk.1 |
| [S13](S13-links-between-projects.md) | How is project A connected to project B? | Team lead, engineer | partial | memini-qpk.8 |

### Decisions

| Id | Question | Who | Status | Issue |
|---|---|---|---|---|
| [S4](S04-what-was-decided.md) | What was decided about X? | Team lead | works |  |
| [S5](S05-latest-decisions-of-a-domain.md) | What are the latest decisions about domain X? | Team lead | partial | memini-qpk.6 |

### My context

| Id | Question | Who | Status | Issue |
|---|---|---|---|---|
| [S6](S06-my-meetings-and-messages.md) | Which meetings did I attend, and what was sent to me? | Team lead | works |  |

### Running Memini

| Id | Question | Who | Status | Issue |
|---|---|---|---|---|
| [S11](S11-run-without-a-terminal.md) | Run Memini without a terminal | Operator | planned | memini-qpk.5 |
| [S12](S12-see-and-edit-the-project-map.md) | See the project map and correct it | Team lead | planned | memini-qpk.5 |
| [S15](S15-see-what-memini-holds.md) | See what Memini holds and try its tools | Operator, team lead | planned | memini-qpk.9 |

## Tools and filters the scenarios need

The same scenarios read from the side of the API: what each tool must accept, and which scenario
asks for it. A filter no scenario asks for is not built.

| Tool | In 0.1.0 | Planned | Scenarios |
|---|---|---|---|
| `search_code` | `query`, `mode` (semantic, exact, regex, symbol), `repositories`, `paths`, `languages` | `projects`, `domains`, `services` | [S1](S01-where-is-x-implemented.md), [S2](S02-code-for-a-domain.md), [S8](S08-several-companies-and-projects.md) |
| `search` | `query`, `kinds`, `sources`, `repositories`, `persons`, `roles`, `since`, `until`, `mode` | `projects`, `domains`, `services` | [S1](S01-where-is-x-implemented.md), [S4](S04-what-was-decided.md), [S2](S02-code-for-a-domain.md), [S8](S08-several-companies-and-projects.md) |
| `get_symbol` | `name`, `match`, `repositories`, `paths`, `languages` |  | [S1](S01-where-is-x-implemented.md), [S10](S10-definitions-in-my-language.md) |
| `read_source` | `repository`, `path`, `ref`, `start_line`, `end_line` | `context` in the answer | [S1](S01-where-is-x-implemented.md), [S3](S03-what-code-belongs-to.md), [S9](S09-project-map-by-an-agent.md) |
| `list_decisions` | `query`, `persons`, `since`, `until`, `history`, `limit` | `projects`, `domains`; newest first without a query | [S4](S04-what-was-decided.md), [S5](S05-latest-decisions-of-a-domain.md) |
| `get_decision` | the item's id | `context` in the answer | [S4](S04-what-was-decided.md), [S5](S05-latest-decisions-of-a-domain.md) |
| `record_decision` | statement, evidence, participants, the items it replaces | `domains` | [S5](S05-latest-decisions-of-a-domain.md) |
| `list_meetings`, `list_messages` | `person`, `roles`, `query`, `since`, `until`, `sources` |  | [S6](S06-my-meetings-and-messages.md) |
| push of records | the record shapes of the push contract | optional `project` and `domains` per record | [S5](S05-latest-decisions-of-a-domain.md), [S8](S08-several-companies-and-projects.md) |
| `get_catalog` |  | new: the workspace's projects, domains, services, repositories without a service | [S2](S02-code-for-a-domain.md), [S8](S08-several-companies-and-projects.md), [S9](S09-project-map-by-an-agent.md), [S12](S12-see-and-edit-the-project-map.md) |
| `get_domain` |  | new: `project`, `domain` | [S2](S02-code-for-a-domain.md), [S5](S05-latest-decisions-of-a-domain.md) |
| `get_project` |  | new: `project`; links in both directions, filter by the other project | [S13](S13-links-between-projects.md) |
| `put_project` |  | new: the project document: services, domains with their code, links | [S9](S09-project-map-by-an-agent.md), [S12](S12-see-and-edit-the-project-map.md), [S13](S13-links-between-projects.md) |
| `catalog_guide` |  | new: no arguments | [S9](S09-project-map-by-an-agent.md) |
| `list_commits` |  | new: `repository`, `paths`, `project`, `domain`, `service`, `author`, `since`, `until`, text | [S7](S07-who-changed-what.md) |

## Not pursued

A scenario that was considered and dropped keeps its number, so it is not proposed again without a
new reason.

| Id | Question | Dropped | Why | Issue |
|---|---|---|---|---|
| S14 | Who calls this function? | 2026-11-23 | Callers can be found in the code itself; `search_code` in exact mode already finds a name across repositories. A reference index would need its own queries per language. | memini-qpk.3 (closed) |

## Add a scenario

1. Copy [TEMPLATE.md](TEMPLATE.md) to `SNN-short-name.md` with the next free number.
2. Fill every part. Say how the data gets in before you design the answer.
3. Add a row to its area above, and to the tools table when it needs a tool or a filter.
4. Name the scenario in the issue that will deliver it.

`tests/test_scenarios_docs.py` checks that every scenario file has its parts and that this index
lists each one with the same title, status and issue.
