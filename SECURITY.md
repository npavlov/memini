# Security policy

## Supported versions

There are no released versions yet. Until v0.1.0, only the `main` branch is supported, and fixes land
there.

| Version | Supported |
|---|---|
| `main` | Yes |

## Report a vulnerability

Do not open a public issue, pull request or discussion for a vulnerability.

Report it privately through GitHub: open <https://github.com/npavlov/memini/security/advisories/new>, or
press "Report a vulnerability" on the Security tab of the repository.

Include:

- the commit or version you tested;
- what an attacker can do, and what they need first (a token, a scope, network access, a crafted record);
- steps to reproduce, with synthetic data only: no real messages, meeting notes, tokens or keys;
- a fix or mitigation, if you have one.

A concern about the design alone, with no code behind it yet, can go in a public issue.

## What to expect

Memini has one maintainer, so these are best-effort targets, not guarantees.

- An acknowledgement within 7 days.
- An assessment and, if the report is valid, a plan for a fix.
- A disclosure date agreed with you. The fix is published with a GitHub security advisory.
- Credit in the advisory, unless you ask not to be named.

## Scope

Areas of most interest, from [docs/requirements.md](docs/requirements.md):

- **Tokens and authorization.** Bearer tokens, their scopes (`read`, `ingest`, `admin`) and the single
  authorization filter that every read goes through. A token that reaches another workspace, or a write
  without the `ingest` scope, is in scope.
- **Redaction at ingest.** Secrets and card numbers that pass redaction and end up stored, indexed or
  returned.
- **Prompt injection through retrieved content.** Retrieved text is data, never instructions. Content in a
  repository, message or meeting that steers the derive stage, or the agent reading search results, is in
  scope.
- **Data leaving the store.** Messages and meetings that leave the service other than by the user's export:
  a cloud LLM call where the source policy forbids it, raw content in logs or run reports, a service
  reachable beyond localhost by default.

A bug in a dependency that does not affect Memini in a specific way belongs with that project.
