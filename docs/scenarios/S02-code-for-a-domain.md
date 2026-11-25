# S2. Which code is responsible for domain X?

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | Engineer, team lead | planned | memini-qpk.1 |

## Who and pain

Search finds pieces of code but does not say which business area they serve. A person who starts
from the business area has nowhere to start.

## Question

"Which code is responsible for berth fees, and in which repositories?"

## Answer

`get_domain(project, domain)`: the description, the services, their repositories, the path masks and
how many files each mask matches. `search_code` and `search` take the filters `projects`, `domains`
and `services`; a domain includes its subdomains. In `search`, `projects` and `domains` also bring
the threads, meetings and pushed documents linked to them, so the conversation about a domain
comes with its code.

## Data

The catalog: project, domain with an optional parent (subdomain), service (a repository or a
directory of one) and path masks per domain. An agent writes it with `put_project`, following
`catalog_guide`; a person edits it in the desktop app. Code comes from git sync as in S1. Membership
of a file is computed when a question is asked, so editing the catalog needs no reindex.

## Check

On a two-repository fixture, `get_domain` lists both services, and a search filtered by a subdomain
returns only files under its masks. A benchmark of a small domain in a large index, before the step
is done.

## See also

- [All scenarios](README.md)
- [S3](S03-what-code-belongs-to.md): What does this piece of code belong to?
- [S9](S09-project-map-by-an-agent.md): The project map without drawing it by hand
- [S5](S05-latest-decisions-of-a-domain.md): What are the latest decisions about domain X?
- Tool reference: [search_code](../mcp/search_code.md)
- Tool reference: [search](../mcp/search.md)
