# S3. What does this piece of code belong to?

| Area | Who | Status | Issue |
|---|---|---|---|
| Catalog: what code and conversations belong to | Engineer | ready | memini-qpk.1 |

## Who and pain

A hit from an unfamiliar repository says nothing about what it is part of.

## Question

"What is this file part of?"

## Answer

Every hit, definition and `get_file` answer carries `context {project, service, domains}`. A file
may belong to several subdomains or to none.

## Data

The catalog of S2. Nothing is stored on the chunks.

## Check

A hit of a file that two subdomains name lists both; a file outside every mask has an empty list.

## See also

- [All scenarios](README.md)
- [S2](S02-code-for-a-domain.md): Which code is responsible for domain X?
- Tool reference: [search_code](../mcp/search_code.md)
- Tool reference: [get_file](../mcp/get_file.md)
