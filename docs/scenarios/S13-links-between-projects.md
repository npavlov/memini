# S13. How is project A connected to project B?

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | Team lead, engineer | partial | memini-qpk.8 |

## Who and pain

A change in one project breaks another, and nobody sees the connection beforehand.

## Question

"How is the port project connected to the platform project? Where does it use it?"

## Answer

`get_project(A)` (`GET /v1/workspaces/{workspace}/projects/{project}`) lists its links in both
directions: `uses`, what its services use, and `used_by`, the services of other projects that use
its services. Each link names both services and their projects, the kind (`library`, `http`,
`queue`, `other`), a note, and the places in the code of the using service that show the use, each
mask with the files it matches now. `other_project` keeps the links between A and that project.
`get_catalog` shows each link under the service that uses it.

## Data

In 0.2 links are declared. The agent that fills the catalog names the services a service uses
(`uses`), in its own or another project of the same workspace, as part of the `put_project`
document, following `catalog_guide`; the same document sent twice changes nothing. A link to a
service that is not in the workspace is refused and named by its place in the document. Partial:
editing links in the desktop app comes with its project map (S12). Later, not in 0.2: links read from dependency manifests
(`package.json`, `go.mod`, `Gemfile`, `pyproject.toml`) by git sync.

## Check

On a two-project fixture, a declared link is returned by `get_project` of both projects, in the
right direction; a link into another workspace is refused; a link to a service that does not exist
is reported by `put_project`; the same document twice changes nothing
(`tests/test_catalog_db.py`, Links between services).

## See also

- [All scenarios](README.md)
- [S9](S09-project-map-by-an-agent.md): The project map without drawing it by hand
- [S12](S12-see-and-edit-the-project-map.md): See the project map and correct it
