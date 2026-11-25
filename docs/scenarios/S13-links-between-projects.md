# S13. How is project A connected to project B?

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | Team lead, engineer | planned | memini-qpk.8 |

## Who and pain

A change in one project breaks another, and nobody sees the connection beforehand.

## Question

"How is the port project connected to the platform project? Where does it use it?"

## Answer

`get_project(A)` lists its links in both directions: which service uses which, the kind of link
(library, HTTP API, queue, other), a note, and the places in the code that show the use. A filter by
the other project.

## Data

In 0.2 links are declared. The agent that fills the catalog names the services a service uses, in
its own or another project of the same workspace, as part of the `put_project` document; a person
edits them in the desktop app. Later, not in 0.2: links read from dependency manifests
(`package.json`, `go.mod`, `Gemfile`, `pyproject.toml`) by git sync.

## Check

On a two-project fixture, a declared link is returned by `get_project` of both projects, in the
right direction; a link into another workspace is refused.

## See also

- [All scenarios](README.md)
- [S9](S09-project-map-by-an-agent.md): The project map without drawing it by hand
- [S12](S12-see-and-edit-the-project-map.md): See the project map and correct it
