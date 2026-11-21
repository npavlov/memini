# Security model

Memini v1 serves a single principal. A client can hold a token restricted to one workspace.
The host operator controls the process, Postgres, repository mirrors, backups, model endpoints
and the credential encryption key. Anyone with host-level access can read source content and
obtain runtime credentials. A database dump contains stored content and token hashes. Keep the
encryption key separately from dumps that contain encrypted source credentials.

## Trust boundaries

| Boundary | Asset and attacker | Enforcement |
| --- | --- | --- |
| Client → HTTP or MCP | A token holder tries to read another workspace or change data. | Authentication resolves the bearer digest. Routes and tools require operation scopes. Reads construct SQL with the caller's workspace filter. Restricted administrative and intake operations also limit the workspace. An unrestricted v1 token can reach every workspace within its scopes. |
| Git repository → mirror → client | A repository author controls paths, link targets and file content. | Git runs with constrained transports and hooks disabled. Local repositories require configured roots. Reads use Git objects at allowed revisions. Symlinks return their target as redacted text. Submodules return the pinned commit. Raw mirrors require host access controls. |
| MCP source → service worker → network | A source administrator chooses endpoints. A remote server controls responses and can redirect requests. | HTTP and OAuth endpoints require an operator allowlist. The network transport checks every resolved address and connects to a checked address. Internal ranges require explicit permission. The client limits redirects, calls, time and response sizes. |
| Source data → model or agent | Repository authors, message participants and remote MCP servers can place instructions in retrieved text. | Tool outputs label provenance and fence source text. Extraction accepts a bounded structured response and validates citations. Journal writes require the ingest scope. An agent holding write privileges must obtain user intent independently of retrieved text. |
| Service → model provider | Provider operators receive the inputs sent to their endpoint. | The LLM gate checks workspace and source policies on each call, including mixed-source calls. Cloud generation requires both permissions. The operator designates the local provider and separately configures the embedding endpoint, which processes indexed text and search queries. |
| Runtime → storage and diagnostics | Logs, errors, run reports and backups can expose source credentials to readers with operational access. | Source credentials use AES-256-GCM. Redaction covers recognized secret formats and registered runtime secrets. The service masks sensitive log fields and omits raw provider errors in client responses. Keep raw mirrors, backups and operational logs within the operator's trust boundary. |

## Review evidence

- **Workspace isolation.** The route and tool inventory exercises filters against Postgres, including the SQL predicate used for reads. Separate tests cover scoped queries against populated workspaces and check data-table scope columns. [Authorization](../tests/test_auth_db.py), [query isolation](../tests/test_query_db.py), [schema](../tests/test_schema.py).
- **Path traversal and resource exhaustion.** Source reads validate paths and revisions before reading pinned objects. Regex search runs inside a transaction with a statement timeout. Request and query limits bound concurrent work and response size. [Source reads](../src/memini/query/reader.py), [Postgres search tests](../tests/test_search_db.py), [API limits](../tests/test_api_limits.py).
- **SSRF and provider disclosure.** The executor tests cover mixed private/public DNS answers, address changes and cross-origin redirects. The LLM gate tests exercise refusals and changes in source policy. [Executor tests](../tests/test_executor.py), [provider policy tests](../tests/test_llm_db.py).
- **Prompt injection and secret disclosure.** The synthetic injection corpus checks output boundaries and extraction constraints. Log and repository tests check that known secret material stays out of client-visible content and diagnostics. This review found an unredacted Git symlink target in `read_source`. It now uses the same redactor as file content. [Injection tests](../tests/test_injection_db.py), [log tests](../tests/test_logs.py), [repository redaction test](../tests/test_redaction_db.py).

## Deployment-dependent findings

The review found two medium-severity risks in deployments that delegate source administration. The stdio fix is available through an operator setting. Git network enforcement remains a deployment control.

| Finding | Exposure and current control | Follow-up |
| --- | --- | --- |
| Git network destinations | Restricted HTTPS mode checks the origin before starting Git, disables redirects, and forces every fetch through the operator’s filtering CONNECT proxy. The proxy enforces approved destination addresses and ports. Local Git keeps its root checks. | Fixed, `memini-qlo.1` |
| Stdio MCP arguments | Set `mcp_allowed_invocations` to authorize exact executable paths, argument vectors and credential environment names. Profiles take precedence over unrestricted executable entries and apply again before each process starts. Both lists default to empty. | Fixed, `memini-qlo.2` |

Report suspected vulnerabilities through the [security reporting policy](../SECURITY.md).

## Restricted Git egress for delegated administrators

Set `MEMINI_GIT_EGRESS_PROXY` to an operator-managed HTTP(S) CONNECT proxy and
`MEMINI_GIT_ALLOWED_ORIGINS` to the exact HTTPS origins Git may fetch. For example,
`http://git-proxy:3128` and `https://git.company.example:8443`. The worker checks the origin on
source creation and again on fetch, disables HTTP redirects, clears inherited proxy overrides,
and refuses network SSH and plaintext HTTP in this mode. An empty origin list with a proxy blocks
network Git. Local sources still require an approved root. The independent
`MEMINI_GIT_HTTPS_TOKEN_HOSTS` list controls delivery of the Git credential.

The proxy is a trusted enforcement point. Allow CONNECT only from the worker to the approved
host and port, and constrain the actual outbound socket to an approved address and port. For
a self-hosted repository, pin its hostname to its intended private address at the proxy and
allow that address explicitly. Apply a default-deny outbound firewall to the proxy, including
IPv6. The proxy must have no alternate forwarding path outside this policy.

For example, a dedicated Squid 6/7 proxy for one internal server can use the following access
rules. Replace the example addresses, and allow the proxy's outbound TCP connections only to
`10.10.4.18:8443` in the deployment firewall. Put `10.10.4.18 git.company.example` in the
proxy's `/etc/squid/git-hosts` and reload both the ACL and the mapping when the server moves.

```conf
hosts_file /etc/squid/git-hosts
acl git_worker src 10.10.2.7/32
acl git_origin dstdomain -n git.company.example
acl git_address dst 10.10.4.18/32
acl git_port port 8443
http_access allow git_worker CONNECT git_origin git_address git_port
http_access deny all
```

Use these as the dedicated proxy’s complete access policy. Restrict who can edit the settings,
proxy and firewall. See [Squid ACLs](https://www.squid-cache.org/Doc/config/acl/),
[access rule order](https://www.squid-cache.org/Doc/config/http_access/) and
[static address mappings](https://www.squid-cache.org/Doc/config/hosts_file/).
