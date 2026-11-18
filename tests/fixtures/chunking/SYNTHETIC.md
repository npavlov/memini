# SYNTHETIC DATA

Everything in this directory is invented and written by hand for the chunking tests of Memini.

- The source files, the handbook, and the document, thread and meeting payloads (`document_*.json`,
  `thread_*.json`, `meeting_*.json`) are fictional, and so are the shops, greenhouses and people in
  them; nothing is derived from real code, messages or meeting notes.
- The other `*.json` files are golden chunks the chunker writes from these inputs
  (`MEMINI_UPDATE_GOLDENS=1`).
- No credentials, tokens or keys.

Keep it that way: a change to this directory adds invented content only (CLAUDE.md, "Never commit").
