# S8. Several companies, several projects each

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | Freelancer | ready | memini-qpk.1 |

## Who and pain

A freelancer keeps the work of several companies in one Memini; inside one company there are several
projects. Nothing may mix, and a search must stay inside one project.

## Question

"Search only in the port project of this client."

## Answer

A workspace per company; a token limited to one workspace sees nothing else. The filter `projects`
on search and on the lists; `get_catalog` shows the projects of a workspace.

## Data

The operator creates workspaces and tokens. Whoever pushes a record chooses the workspace it goes
to. Projects come from `put_project` (S9).

## Check

The existing access tests for workspaces; a fixture for the project filter.

## Notes

Workspaces work today; projects are planned.

## See also

- [All scenarios](README.md)
- [S9](S09-project-map-by-an-agent.md): The project map without drawing it by hand
- [S5](S05-latest-decisions-of-a-domain.md): What are the latest decisions about domain X?
- Tool reference: [search](../mcp/search.md)
- Tool reference: [search_code](../mcp/search_code.md)
