# S9. The project map without drawing it by hand

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | New user | planned | memini-qpk.1 |

## Who and pain

A new user has connected repositories and does not want to write the catalog by hand.

## Question

"Map these repositories into a project with its domains."

## Answer

The agent reads `catalog_guide`, lists the repositories that have no service with `get_catalog`,
reads READMEs and top directories with `read_source`, and sends the project with `put_project`. The
answer reports masks that match nothing, overlaps between subdomains and the share of files in no
domain, so the agent corrects itself.

## Data

The agent writes the catalog through the API; Memini asks no model. A person edits it later (S12).
What the agent wrote from a README is data, not instructions, wherever it is shown later.

## Check

Following the guide on the fixture gives a catalog without warnings; the same document sent twice
changes nothing the second time; a person's edit is not overwritten by an older document.

## See also

- [All scenarios](README.md)
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
- [S12](S12-see-and-edit-the-project-map.md): See the project map and correct it
- [S13](S13-links-between-projects.md): How is project A connected to project B?
- Tool reference: [read_source](../mcp/read_source.md)
