# Configuration reference

<!-- Generated from src/memini/config.py by `make config-docs` (src/memini/config_reference.py). Do not edit by hand: a test fails while this file and the code differ. -->

Every setting of the service and of its operator commands (`memini serve`, `migrate`, `bootstrap`, `reindex`, `credentials`, `workspace llm`, `eval`), with its default and what a change of it needs; then the variables of the client commands and of the compose stack.

## Where settings come from

Highest precedence first:

1. The environment: `MEMINI_` and the setting name in upper case, so `http_port` is `MEMINI_HTTP_PORT`. A variable set to the empty string counts as not set; other `MEMINI_*` variables are ignored.
2. A TOML file, if `MEMINI_CONFIG_FILE` holds its path (operator commands also take `--config FILE`). Keys are setting names at the top level; an unknown key is an error, so a typo does not go unnoticed.
3. The defaults below.

```toml
log_level = "DEBUG"
http_port = 6364
git_local_roots = ["/srv/repos", "/home/me/src"]
git_max_blob_size = "2M"
```

**Secrets** come from the environment only, as `MEMINI_NAME` (the value) or `MEMINI_NAME_FILE` (the path of a file that holds it, as Docker and compose secrets provide it; trailing line breaks are dropped). Setting both is an error, and so is a secret in the TOML file, which holds nothing secret and can be shared. No log line, error message or output holds a secret's value.

**Lists** are TOML arrays in the file, or comma-separated in the environment (`MEMINI_GIT_LOCAL_ROOTS=/srv/repos,/home/me/src`). **Sizes** are a number of bytes, or a number with a binary unit: `512M`, `5G`, `1GiB` (1K = 1024 bytes).

Settings are checked together at start. A missing or invalid one stops the service, or the operator command, with exit code 2 and one line per problem; each line names the setting and never its value.

## What a change needs

- **restart**: the service reads its settings once, at start. With compose, `docker compose up -d` recreates the container when its environment changed. Operator commands read the settings on every run.
- **restart, then `memini reindex`**: the setting is part of the index fingerprint: `embedding_provider`, `embedding_model`, `embedding_dimension`, `embedding_digest`, `embedding_document_prefix`, `embedding_pooling`, `embedding_max_tokens`. After a change, search and indexing in a workspace whose index was built with other values are refused, with a message that names the command, until `memini reindex --workspace NAME` rebuilds that index.
- A setting with a procedure of its own says so in the table.

## Settings

### Database

The Postgres database, with pgvector, that the service and the operator commands use. Backup is a dump of it; the mirrors of git sources are fetched again.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `database_url`<br>`MEMINI_DATABASE_URL`<br>`MEMINI_DATABASE_URL_FILE`<br>*secret* | string, not empty | **required** | restart | A Postgres connection URL. Compose builds it from `MEMINI_DB_PASSWORD`. |

### HTTP server and request limits

Where the server listens, and the limits of every HTTP and MCP request. A body over its limit is refused by its `Content-Length` before it is read, or as soon as more of it arrives (413); the ingest route has its own limit, `ingest_max_body_size`. The statement limits apply to the database statements a request runs; search has its own, `search_timeout`. Rate limits: per token, a budget of reads and one of writes; per client address, one for requests whose token is not known to be valid. Each budget is a minute's worth of requests, refilled steadily, and is kept in the process: budgets start full after a restart.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `http_host`<br>`MEMINI_HTTP_HOST` | string, not empty | `127.0.0.1`; in the image: `0.0.0.0` | restart | The address the HTTP server binds to. |
| `http_port`<br>`MEMINI_HTTP_PORT` | integer, 1 to 65535 | `6364`; in the image: `6364` | restart | A TCP port from 1 to 65535. The HTTP API and MCP (`/mcp`) listen on it. |
| `http_max_body_size`<br>`MEMINI_HTTP_MAX_BODY_SIZE` | size, 1K to 1G | `1M` | restart | The largest request body outside the ingest and raw response routes, 1K to 1G: bytes, or a number with K, M or G. |
| `http_statement_timeout`<br>`MEMINI_HTTP_STATEMENT_TIMEOUT` | number, 0 to 86400 | `30` | restart | Seconds one database statement of a request may run, 0 to 86400; 0: the database's default. |
| `http_idle_in_transaction_timeout`<br>`MEMINI_HTTP_IDLE_IN_TRANSACTION_TIMEOUT` | number, 0 to 86400 | `300` | restart | Seconds a request's transaction may stay idle, 0 to 86400; 0: the database's default. |
| `http_rate_limit`<br>`MEMINI_HTTP_RATE_LIMIT` | boolean | `true` | restart | True or false: limit requests per token and per client address. |
| `http_rate_limit_reads`<br>`MEMINI_HTTP_RATE_LIMIT_READS` | integer, 1 to 1000000 | `600` | restart | Reads (GET, and every MCP request but a call of a write tool) per minute per token, 1 to 1000000. |
| `http_rate_limit_writes`<br>`MEMINI_HTTP_RATE_LIMIT_WRITES` | integer, 1 to 1000000 | `120` | restart | Writes (POST, PUT, PATCH, DELETE, and MCP calls of write tools) per minute per token, 1 to 1000000. |
| `http_rate_limit_unauthenticated`<br>`MEMINI_HTTP_RATE_LIMIT_UNAUTHENTICATED` | integer, 1 to 1000000 | `60` | restart | Requests per minute per client address whose token is not known to be valid, 1 to 1000000. |

### MCP endpoint

The service's own MCP endpoint, `/mcp` on the HTTP port: the tools agents call, behind the same tokens as the HTTP API. Switched off, it answers every request 404 `mcp_disabled`, with a token or without, and no tool runs; the HTTP API, which serves the same calls, stays. `GET /v1/status` says which it is (`mcp_enabled`). The `mcp_*` settings further down are another matter: the client that syncs mcp sources.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `mcp_enabled`<br>`MEMINI_MCP_ENABLED` | boolean | `true` | restart | True or false: serve the MCP endpoint at /mcp. With the server launcher: `memini-server mcp on` and `memini-server mcp off` set it and restart the service. |

### Logging

The service logs one JSON object per line, on stderr.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `log_level`<br>`MEMINI_LOG_LEVEL` | one of `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG` | `INFO` | restart | One of CRITICAL, ERROR, WARNING, INFO, DEBUG. The lowest level the service logs; any case. |
| `log_slow_statement_threshold`<br>`MEMINI_LOG_SLOW_STATEMENT_THRESHOLD` | number, 0 to 86400 | `1` | restart | Seconds a database statement may take before it is logged as slow, 0 to 86400; 0: no log. A statement of the service over it is logged at WARNING with its duration, the request or run it served, and its text with every literal replaced by `?`, never its parameters. |

### Metrics

`GET /metrics` serves counts and timings in the Prometheus text format, to a scraper that sends `metrics_token` as its bearer token; without the token the route does not exist. A Memini token is not accepted there. No metric is labelled by a workspace, source, stream or person; `memini_base_current` says what `GET /v1/status?gate=true` says.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `metrics_token`<br>`MEMINI_METRICS_TOKEN`<br>`MEMINI_METRICS_TOKEN_FILE`<br>*secret* | string, 32 to 512 characters | unset | restart | A random secret of at least 32 characters, without whitespace, e.g. from `openssl rand -hex 32`. Unset: there is no `/metrics` route. Prometheus sends it with `authorization: {credentials_file: ...}`; it reads counts only. |

### Operator

v1 has one user. The operator commands act as one principal, named here.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `principal`<br>`MEMINI_PRINCIPAL` | string | `owner` | nothing to restart (operator commands only); keep it after the first `memini bootstrap` | 1 to 63 lowercase letters, digits, '-' or '_', starting with a letter or digit. The principal the operator commands act as, created on first use: `memini bootstrap` gives it the first admin token, so it owns what is made with that token. |

### Embeddings

Off until `embedding_provider` is set: the service starts without them, but does not sync git sources, and search refuses with a message that names the setting. `openai_compatible` sends the text of chunks and queries to the API at `embedding_url` (Ollama, llama.cpp, vLLM or a cloud service): which endpoint, on this machine or not, is your explicit choice. `local` runs an ONNX model in the process; it needs the optional dependencies `memini[local-embeddings]` and pins the model files by `embedding_digest`, which is verified before they are loaded. A setting of the provider not chosen, or any `embedding_*` setting without a provider, is an error.

With `embedding_provider = openai_compatible` and nothing else set, these defaults apply (`ollama pull qwen3-embedding:0.6b` first):

- `embedding_url`: `http://127.0.0.1:11434/v1`
- `embedding_model`: `qwen3-embedding:0.6b`
- and while the model is `qwen3-embedding:0.6b`:
  - `embedding_dimension`: `1024`
  - `embedding_digest`: `ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d`
  - `embedding_query_prefix`: `Instruct: Given a search query, retrieve the relevant code or documentation that can help answer the query\nQuery:`

A value that is set wins. Another model gets no dimension, digest or query prefix from here: it needs its own `embedding_dimension`.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `embedding_provider`<br>`MEMINI_EMBEDDING_PROVIDER` | one of `openai_compatible`, `local` | unset | restart, then `memini reindex` | openai_compatible or local. Unset: embeddings are off. |
| `embedding_model`<br>`MEMINI_EMBEDDING_MODEL` | string, 1 to 200 characters | unset; with `openai_compatible`: `qwen3-embedding:0.6b`; required with `local` | restart, then `memini reindex` | A model name; for local, a Hugging Face repository such as org/name. |
| `embedding_dimension`<br>`MEMINI_EMBEDDING_DIMENSION` | integer, 1 to 16000 | unset; with `openai_compatible` and its default model: `1024`; required with any provider | restart, then `memini reindex` | The model's vector size, an integer from 1 to 16000. |
| `embedding_digest`<br>`MEMINI_EMBEDDING_DIGEST` | string, 1 to 200 characters | unset; with `openai_compatible` and its default model: see above; required with `local` | restart, then `memini reindex` | For local, sha256: and 64 lowercase hex digits; for openai_compatible, the version of the served model, such as its Ollama digest. For `local` it is verified before the model files are loaded. For `openai_compatible` it is recorded, not checked: after `ollama pull` brings a new build, set its digest. |
| `embedding_batch_size`<br>`MEMINI_EMBEDDING_BATCH_SIZE` | integer, 1 to 2048 | `32` | restart | Texts per batch, an integer from 1 to 2048. |
| `embedding_timeout`<br>`MEMINI_EMBEDDING_TIMEOUT` | number, more than 0, at most 3600 | `60` | restart | Seconds per request, more than 0, at most 3600. |
| `embedding_query_prefix`<br>`MEMINI_EMBEDDING_QUERY_PREFIX` | string, at most 200 characters | empty; with `openai_compatible` and its default model: see above | restart | At most 200 characters, e.g. 'query: '. Put before every search query before it is embedded. |
| `embedding_document_prefix`<br>`MEMINI_EMBEDDING_DOCUMENT_PREFIX` | string, at most 200 characters | empty | restart, then `memini reindex` | At most 200 characters, e.g. 'passage: '. Put before every chunk before it is embedded. |
| `embedding_url`<br>`MEMINI_EMBEDDING_URL` | string, at most 500 characters | unset; with `openai_compatible`: `http://127.0.0.1:11434/v1` | restart | *`openai_compatible` only.* The base URL of an OpenAI-compatible API, e.g. http://127.0.0.1:11434/v1 (the default). It receives the text of every chunk and query. |
| `embedding_api_key`<br>`MEMINI_EMBEDDING_API_KEY`<br>`MEMINI_EMBEDDING_API_KEY_FILE`<br>*secret* | string, not empty | unset | restart | *`openai_compatible` only.* Sent to `embedding_url` in the `Authorization` header. |
| `embedding_model_dir`<br>`MEMINI_EMBEDDING_MODEL_DIR` | path | unset; required with `local` | restart | *`local` only.* A directory for the model files; created if missing. The files of each digest get a directory of their own in it. |
| `embedding_model_file`<br>`MEMINI_EMBEDDING_MODEL_FILE` | string, at most 200 characters | `onnx/model.onnx` | a new `embedding_digest`, which covers the file's path; so restart, then `memini reindex` | *`local` only.* The path of the ONNX file in the model repository, e.g. onnx/model.onnx. The digest covers this file and `tokenizer.json`, with their paths. |
| `embedding_revision`<br>`MEMINI_EMBEDDING_REVISION` | string | unset | restart | *`local` only.* A commit of the model repository, 40 lowercase hex digits. Missing model files are fetched at this commit; without it, they must be in place. |
| `embedding_pooling`<br>`MEMINI_EMBEDDING_POOLING` | one of `mean`, `cls` | `mean` | restart, then `memini reindex` | *`local` only.* mean or cls. How the token vectors of a text become one: their mean, or the vector of the first token. |
| `embedding_max_tokens`<br>`MEMINI_EMBEDDING_MAX_TOKENS` | integer, 1 to 32768 | `512` | restart, then `memini reindex` | *`local` only.* An integer from 1 to 32768. A longer text is cut to this many tokens. |

### Search

The limits of one search, and how a hybrid search fuses its retrievers: each contributes `search_candidates` results, merged by reciprocal rank fusion with k `search_rrf_k`, each retriever's ranks weighted. The reranker (what it gains and costs: docs/requirements.md, section 9, Reranker) is `none` by default and `local` in the image, which carries its runtime and its files, about 610 MB; they are verified against `search_reranker_digest` each time the service starts, before they are loaded. Outside the image, `local` needs the optional dependencies `memini[local-embeddings]`, and its files are fetched at the pinned revision when the service first starts with it. A `search_reranker_*` setting while `search_reranker` is `none` is an error, but for `search_reranker_model_dir`. Another model or model file needs its own digest, and another model its own revision; in the image it also needs a writable `search_reranker_model_dir`, such as a volume, as the image's own is read-only.

The pinned default uses per-token INT8 on Linux arm64 and on Linux x86-64 where every CPU reports AVX-512 VNNI. Other Linux x86-64 CPUs use per-channel Q7 weights, including when CPU flags are unavailable. Both graphs read the same verified weights from the cache. Other platforms and model digests use the supplied model graph. [Graph provenance](../packaging/reranker/README.md)

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `search_timeout`<br>`MEMINI_SEARCH_TIMEOUT` | number, more than 0, at most 600 | `5` | restart | Seconds one search statement may run, more than 0, at most 600. |
| `search_candidates`<br>`MEMINI_SEARCH_CANDIDATES` | integer, 1 to 1000 | `50` | restart | Candidates per retriever of a hybrid search, an integer from 1 to 1000. |
| `search_rrf_k`<br>`MEMINI_SEARCH_RRF_K` | integer, 1 to 1000 | `1` | restart | The k of reciprocal rank fusion, an integer from 1 to 1000. |
| `search_dense_weight`<br>`MEMINI_SEARCH_DENSE_WEIGHT` | number, more than 0, at most 100 | `3` | restart | The weight of the dense retriever's ranks in the fusion, more than 0, at most 100. |
| `search_lexical_weight`<br>`MEMINI_SEARCH_LEXICAL_WEIGHT` | number, more than 0, at most 100 | `1` | restart | The weight of the lexical retriever's ranks in the fusion, more than 0, at most 100. |
| `search_reranker`<br>`MEMINI_SEARCH_RERANKER` | one of `local`, `none` | `none`; in the image: `local` | restart | local: a cross-encoder run in the process; none: no reranking. The model loads in the background at start; until it is loaded, and if it cannot load, search keeps the fused order, and the log says why once. Under compose, `MEMINI_SEARCH_RERANKER=none` in the settings file turns it off. |
| `search_rerank_depth`<br>`MEMINI_SEARCH_RERANK_DEPTH` | integer, 1 to 100 | `3` | restart | How many of the best fused hits the reranker reorders, an integer from 1 to 100. Hits below it keep their fused place. `memini eval --reranker` reads it too, so it may be set while `search_reranker` is `none`. |
| `search_reranker_model`<br>`MEMINI_SEARCH_RERANKER_MODEL` | string, 1 to 200 characters | `n24q02m/Qwen3-Reranker-0.6B-ONNX` | restart | A Hugging Face repository such as org/name. |
| `search_reranker_model_file`<br>`MEMINI_SEARCH_RERANKER_MODEL_FILE` | string, at most 200 characters | `onnx/model_yesno_quantized.onnx` | restart | The path of the ONNX file in the model repository. The digest covers this file and `tokenizer.json`, with their paths. |
| `search_reranker_digest`<br>`MEMINI_SEARCH_RERANKER_DIGEST` | string | `sha256:eaa6edfadd2440e4f80851b3ff7701f26e8ec6aacaa2fedcee8ce86afd09afea` | restart | sha256: and 64 lowercase hex digits, the digest of the model files. Files that do not match are never loaded. |
| `search_reranker_revision`<br>`MEMINI_SEARCH_RERANKER_REVISION` | string | `342a8a54de23bba03eb8cce5f5bef503edf18315` | restart | A commit of the model repository, 40 lowercase hex digits. Missing model files are fetched at this commit. |
| `search_reranker_model_dir`<br>`MEMINI_SEARCH_RERANKER_MODEL_DIR` | path | unset; in the image: `/opt/memini/models` | restart | An absolute directory path for the model files, created if missing; unset: $XDG_CACHE_HOME/memini/models, or ~/.cache/memini/models. In the image it holds the default model's files, read-only. |
| `search_reranker_max_tokens`<br>`MEMINI_SEARCH_RERANKER_MAX_TOKENS` | integer, 64 to 8192 | `1024` | restart | The most tokens of one query and hit put to the model, 64 to 8192. A longer prompt is cut at the end of the hit. Each token costs time: about 0.5 ms on an Apple M4 Max. |
| `search_reranker_timeout`<br>`MEMINI_SEARCH_RERANKER_TIMEOUT` | number, more than 0, at most 600 | `5` | restart | Seconds the reranking of one search may take, more than 0, at most 600. Past it, the search answers in the fused order and logs a warning. |
| `search_reranker_threads`<br>`MEMINI_SEARCH_RERANKER_THREADS` | integer, 0 to 256 | `0` | restart | Threads of one model run, 0 to 256; 0: as many as the CPUs or a CPU limit of the container allow, at most 4. onnxruntime would take one per core, and each step of a run waits for the slowest thread: on a machine shared with other work, fewer threads answer sooner. The log line `reranking with ...` says how many. |

### Git connector

The service keeps a bare mirror of every git source and reads code from it. Remote sources use https or ssh; plain http and local paths are refused unless allowed here. Credentials are global for now: one https token and one ssh key.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `git_mirror_dir`<br>`MEMINI_GIT_MIRROR_DIR` | path | unset; in the image: `/var/lib/memini/mirrors` | restart; mirrors are not moved: the next sync of each source fetches its mirror anew | An absolute directory path; unset: $XDG_DATA_HOME/memini/mirrors, or ~/.local/share/memini/mirrors (the image sets /var/lib/memini/mirrors). |
| `git_fetch_timeout`<br>`MEMINI_GIT_FETCH_TIMEOUT` | integer, 1 to 86400 | `600` | restart | Seconds a clone or fetch may take, 1 to 86400. |
| `git_max_mirror_size`<br>`MEMINI_GIT_MAX_MIRROR_SIZE` | size, at least 1 | `5G` | restart | The largest size of one mirror on disk: bytes, or a number with K, M, G or T. |
| `git_max_blob_size`<br>`MEMINI_GIT_MAX_BLOB_SIZE` | size, at least 1 | `1M` | restart | The largest file read from a mirror: bytes, or a number with K, M, G or T. |
| `git_allow_http`<br>`MEMINI_GIT_ALLOW_HTTP` | boolean | `false` | restart | True or false: allow plain http:// remotes (no TLS). |
| `git_egress_proxy`<br>`MEMINI_GIT_EGRESS_PROXY` | string | unset | restart | HTTP(S) CONNECT proxy origin for restricted Git egress; the proxy must enforce destination address and port ACLs; unset: legacy transports. |
| `git_allowed_origins`<br>`MEMINI_GIT_ALLOWED_ORIGINS` | list of strings | empty | restart | Exact HTTPS origins permitted through `git_egress_proxy`, comma-separated; empty with a proxy: deny network Git; requires `git_egress_proxy`. |
| `git_local_roots`<br>`MEMINI_GIT_LOCAL_ROOTS` | list of paths | empty | restart | Absolute directory paths, comma-separated, under which local repositories may be sources; empty: no local sources. With compose, mount the directory into the container as well; compose.yaml has an example. |
| `git_https_token`<br>`MEMINI_GIT_HTTPS_TOKEN`<br>`MEMINI_GIT_HTTPS_TOKEN_FILE`<br>*secret* | string | unset | restart | A token for https remotes, without whitespace. |
| `git_https_username`<br>`MEMINI_GIT_HTTPS_USERNAME` | string | `x-access-token` | restart | The user name sent with `git_https_token`: printable ASCII, no spaces. |
| `git_https_token_hosts`<br>`MEMINI_GIT_HTTPS_TOKEN_HOSTS` | list of strings | empty | restart | Host names, comma-separated, that `git_https_token` is sent to; required when the token is set. |
| `git_ssh_key_path`<br>`MEMINI_GIT_SSH_KEY_PATH` | path | unset | restart | The absolute path of the private key for ssh remotes. |
| `git_ssh_known_hosts_path`<br>`MEMINI_GIT_SSH_KNOWN_HOSTS_PATH` | path | unset | restart | The absolute path of the known_hosts file that ssh remotes are checked against. |

### Ingest

The limits of one batch of records sent to the ingest API: the request body, the records in it, and the payload of one record as stored.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `ingest_max_body_size`<br>`MEMINI_INGEST_MAX_BODY_SIZE` | size, 1K to 1G | `16M` | restart | The largest request body of an ingest batch or of an agent run's raw response, 1K to 1G: bytes, or a number with K, M or G. |
| `ingest_max_records`<br>`MEMINI_INGEST_MAX_RECORDS` | integer, 1 to 100000 | `1000` | restart | The most records and tombstones in one ingest batch, 1 to 100000. |
| `ingest_max_payload_size`<br>`MEMINI_INGEST_MAX_PAYLOAD_SIZE` | size, 1K to 1G | `1M` | restart | The largest payload of one record as stored, 1K to 1G: bytes, or a number with K, M or G. |

### Credential store

The API keys and OAuth tokens of sources are stored encrypted (AES-256-GCM) with `credential_key`, which never enters the database. To rotate it: make a new key (`openssl rand -base64 32`), set it as `credential_key` and move the old one to `credential_old_keys`, and restart; run `memini credentials rotate-key`; when it reports nothing left, remove `credential_old_keys` and restart.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `credential_key`<br>`MEMINI_CREDENTIAL_KEY`<br>`MEMINI_CREDENTIAL_KEY_FILE`<br>*secret* | string | unset | restart with the old key in `credential_old_keys`, then `memini credentials rotate-key` | 32 random bytes in base64, e.g. from `openssl rand -base64 32`. Unset: the service starts, and storing or reading a credential fails with a message that names this setting. |
| `credential_old_keys`<br>`MEMINI_CREDENTIAL_OLD_KEYS`<br>`MEMINI_CREDENTIAL_OLD_KEYS_FILE`<br>*secret* | list of strings | empty | restart; remove them once `memini credentials rotate-key` reports nothing left | Keys still read during a rotation, comma-separated, each 32 random bytes in base64, e.g. from `openssl rand -base64 32`; only together with `credential_key`. |

### MCP service executor

The service syncs mcp sources on its own with the built-in MCP client, and connects only where these settings allow: an endpoint inside `mcp_allowed_endpoints` (https, unless `mcp_allow_http`), whose host resolves to no internal address (loopback, private, link-local, the cloud metadata address) unless its network is in `mcp_allowed_networks`, checked again at every connection and redirect; a stdio server only from an approved command or invocation. Every list is empty by default, so no mcp source reaches anything until it is listed. A source's OAuth token endpoint is an endpoint too.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `mcp_allowed_endpoints`<br>`MEMINI_MCP_ALLOWED_ENDPOINTS` | list of strings | empty | restart | Hosts (mcp.example.com, host:port, *.example.com) or URL prefixes (https://mcp.example.com/v1), comma-separated, that mcp sources may connect to; empty: none. Checked when an mcp source is added or changed, and before every connection; a change applies to the next sync. With compose, a server on the host is `host.docker.internal`, whose address is internal: list its network as well. |
| `mcp_allowed_networks`<br>`MEMINI_MCP_ALLOWED_NETWORKS` | list of strings | empty | restart | Networks in CIDR notation, comma-separated, of internal addresses (loopback, private, link-local) that MCP endpoints may resolve to all the same; empty: none. |
| `mcp_allow_http`<br>`MEMINI_MCP_ALLOW_HTTP` | boolean | `false` | restart | True or false: allow plain http:// MCP endpoints (no TLS). |
| `mcp_allowed_commands`<br>`MEMINI_MCP_ALLOWED_COMMANDS` | list of paths | empty | restart | Absolute paths, comma-separated, of the executables mcp sources may run as stdio servers; empty: none. The path given is the path allowed; a symlink is not resolved. With compose the executable must exist in the container. |
| `mcp_allowed_invocations`<br>`MEMINI_MCP_ALLOWED_INVOCATIONS` | list of StdioInvocations | empty | restart | Approved stdio invocations with command, args and optional credential_env; a JSON array in the environment or an array of tables in TOML. For a listed command, the ordered arguments and credential variable must match one entry exactly, even when mcp_allowed_commands also lists the command. Example: `[{"command":"/opt/mcp","args":["--ro"],"credential_env":"HEARTH_TOKEN"}]`. |
| `mcp_call_attempts`<br>`MEMINI_MCP_CALL_ATTEMPTS` | integer, 1 to 10 | `3` | restart | Attempts of one call of an mcp sync, the first included, 1 to 10. Each try is a call of the recipe's `[rate]` budget. A call that times out, fails on the network, gets HTTP 429 or a server error, or answers `isError`, is tried again after a backoff; one out of tries leaves its window incomplete. |

### LLM providers

At most two OpenAI-compatible chat APIs, declared by the slot they are set in: `llm_local_*` is trusted with the content of every source, `llm_cloud_*` (https only) only with sources whose `llm_policy` is `cloud`, in workspaces that opted in (`memini workspace llm`). The slot is the declaration; it is never inferred from the URL. Each slot needs its URL and its model. No provider is configured by default, and then nothing is extracted.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `llm_local_url`<br>`MEMINI_LLM_LOCAL_URL` | string, at most 500 characters | unset | restart | The base URL of an OpenAI-compatible API you trust with every source's content, e.g. http://127.0.0.1:11434/v1. Unset, and `llm_cloud_url` unset too: no LLM is called. |
| `llm_local_model`<br>`MEMINI_LLM_LOCAL_MODEL` | string, 1 to 200 characters | unset | restart | The model the local API serves. Required when `llm_local_url` is set. |
| `llm_local_api_key`<br>`MEMINI_LLM_LOCAL_API_KEY`<br>`MEMINI_LLM_LOCAL_API_KEY_FILE`<br>*secret* | string, not empty | unset | restart | A key for the local API, without whitespace. Only with `llm_local_url`. |
| `llm_local_structured_output`<br>`MEMINI_LLM_LOCAL_STRUCTURED_OUTPUT` | one of `json_schema`, `json_object`, `none` | `json_schema` | restart | json_schema, json_object or none: how the local API is asked for JSON. Only with `llm_local_url`. The reply is validated against the schema in every case. |
| `llm_cloud_url`<br>`MEMINI_LLM_CLOUD_URL` | string, at most 500 characters | unset | restart | The https:// base URL of an OpenAI-compatible API that may read sources whose llm_policy is cloud, in workspaces that opted in. Not the same endpoint and model as `llm_local_url`. |
| `llm_cloud_model`<br>`MEMINI_LLM_CLOUD_MODEL` | string, 1 to 200 characters | unset | restart | The model the cloud API serves. Required when `llm_cloud_url` is set. |
| `llm_cloud_api_key`<br>`MEMINI_LLM_CLOUD_API_KEY`<br>`MEMINI_LLM_CLOUD_API_KEY_FILE`<br>*secret* | string, not empty | unset | restart | A key for the cloud API, without whitespace. Only with `llm_cloud_url`. |
| `llm_cloud_structured_output`<br>`MEMINI_LLM_CLOUD_STRUCTURED_OUTPUT` | one of `json_schema`, `json_object`, `none` | `json_schema` | restart | json_schema, json_object or none: how the cloud API is asked for JSON. Only with `llm_cloud_url`. The reply is validated against the schema in every case. |
| `llm_timeout`<br>`MEMINI_LLM_TIMEOUT` | number, more than 0, at most 3600 | `300` | restart | Seconds per LLM request, more than 0, at most 3600. Only with `llm_local_url` or `llm_cloud_url`. |

### Decision extraction

With an LLM provider, each thread or meeting that ingest stores new or changed gets an `extract` job: one LLM call extracts its decisions and open questions, each with the messages or segments that say it, and one call per item checks it against the most similar active items of the workspace (a duplicate, or replacing or answering some). A source or workspace whose `llm_policy` is `none` is skipped. Without a provider no job is enqueued.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `extraction_candidates`<br>`MEMINI_EXTRACTION_CANDIDATES` | integer, 1 to 50 | `8` | restart | Active items, 1 to 50, that a new item is checked against for duplicates, replacements and answers. Items whose evidence the provider may not read are left out. A larger number costs more input tokens per check. |
| `extraction_candidate_retriever`<br>`MEMINI_EXTRACTION_CANDIDATE_RETRIEVER` | one of `lexical`, `hybrid` | `hybrid` | restart | lexical or hybrid: how those items are found, by the words of the statements or by their words and their vectors. `hybrid` needs an embedding provider and is `lexical` without one; it embeds each statement once, with the configured model, and keeps the vector with its item. |

### Other settings

Settings that no group above takes yet.

| Setting | Values | Default | Change needs | Description |
| --- | --- | --- | --- | --- |
| `retention_interval`<br>`MEMINI_RETENTION_INTERVAL` | integer, 60 to 604800 | `3600` | restart | Seconds between two retention passes over a source that has retention_days, 60 to 604800. |
| `retention_batch_size`<br>`MEMINI_RETENTION_BATCH_SIZE` | integer, 1 to 100000 | `1000` | restart | Records one transaction of a retention pass deletes at most, 1 to 100000. |

## Client commands

Client commands (`memini workspace create`, `source`, `sync`, `runs`, `token`, `search` and the others that are not operator commands) are HTTP clients of the running service. They read these variables from the environment, and nothing else: not the service's settings, nor its config file.

| Variable | Default | Description |
| --- | --- | --- |
| `MEMINI_URL` | `http://127.0.0.1:6364` | The service's base URL, `http://` or `https://`, and TLS is whatever the URL says. A URL with a user name or password is refused: the token is the credential. `--url` overrides it. |
| `MEMINI_TOKEN` | **required**, or `MEMINI_TOKEN_FILE` | The token, a secret: sent as `Authorization: Bearer …`, never printed. No option takes the token itself, which shell history and the process list would keep. An admin creates one with `memini token create`; the first comes from `memini bootstrap`. |
| `MEMINI_TOKEN_FILE` | unset | The path of a file that holds the token; trailing line breaks are dropped. Setting both it and `MEMINI_TOKEN` is an error. `--token-file` overrides both. |
| `MEMINI_CLIENT_TIMEOUT` | 60 seconds | Positive, finite seconds to wait for HTTP activity. Connection attempts use at most 10 seconds. |

## Docker compose

`compose.yaml` (Docker Compose 2.24.0 or later) takes its configuration from three places, none of them required. After a change, `docker compose up -d` recreates the service.

### What compose reads: `.env`

`compose.yaml` reads these variables from the environment, or from `.env` next to it (`.env.example` lists them), and the paths of the secret files below. They configure compose on the host; none of them is a setting of the service.

| Variable | Default | Description |
| --- | --- | --- |
| `MEMINI_HTTP_PORT` | `6364` | The port on the host's 127.0.0.1 the service is published on; MCP at `http://127.0.0.1:6364/mcp` by default. Not the setting `http_port`: in the container compose sets `MEMINI_HTTP_PORT` to 6364, where the service listens. |
| `MEMINI_DB_PORT` | `55432` | The port on the host's 127.0.0.1 the database is published on. |
| `MEMINI_DB_PASSWORD` | `memini` | The password of the database role, a development default; letters and digits only, as compose puts it into `MEMINI_DATABASE_URL` as it is. Set it before the first start: the database keeps the password it was created with. |
| `MEMINI_ENV_FILE` | `.env.memini` | The settings file, which compose passes into the service's container; relative to `compose.yaml`, or absolute. Optional: a file that does not exist is skipped. |
| `MEMINI_RERANKER_FILES` | `baked` | The reranker's model files in the image compose builds: `baked`, fetched at the build (about 610 MB, once: the layer cache keeps them) and checked against their pinned SHA-256; `none` leaves them out, and the reranker then says in the log that it is off. With `none`, set `MEMINI_SEARCH_RERANKER=none` in the settings file as well. |

### The settings of the service: `.env.memini`

Compose passes every line of `.env.memini`, or of the file `MEMINI_ENV_FILE` names, into the service's container as it is: one `MEMINI_NAME=value` per line, the variables of the settings above. So any setting can be set there, a new one included, without a change of `compose.yaml`; `.env.memini.example` lists them. Its values show in `docker inspect`: a secret goes into a file (below) instead.

Compose sets these itself, over a line of that file: `MEMINI_DATABASE_URL` (the database of the stack, with `MEMINI_DB_PASSWORD`), `MEMINI_HTTP_HOST` (`0.0.0.0`), `MEMINI_HTTP_PORT` (`6364`), `MEMINI_GIT_MIRROR_DIR` (`/var/lib/memini/mirrors`). Inside the container 127.0.0.1 is the container itself: with `embedding_provider`, set `MEMINI_EMBEDDING_URL=http://host.docker.internal:11434/v1` for Ollama on the host. The image has the runtime of in-process models, for the reranker, but no embedding model and no writable place for one, so the `local` embedding provider is not set up there.

### Secrets and other files

Each of these variables, set in `.env`, names a file on the host: relative to `compose.yaml`, or absolute. Compose mounts it read-only into the container as a compose secret and sets the same variable there to the file's path in the container, where the service reads it; the file's content never shows in `docker inspect`. Unset, compose mounts the empty `secrets/unset` instead and leaves the setting unset. The service runs as uid 10001 and must be able to read the file: on Linux it keeps its owner and mode in the container, so give it to that user (`sudo chown 10001 FILE`, mode 400); Docker Desktop and OrbStack let the container's user read it. ssh refuses a private key that others can read: keep the key's mode 600 or 400. Compose does not notice a change of a file's content: `docker compose up -d --force-recreate memini` reads it again.

| Variable | In the container | Setting |
| --- | --- | --- |
| `MEMINI_EMBEDDING_API_KEY_FILE` | `/run/secrets/embedding_api_key` | `embedding_api_key` |
| `MEMINI_GIT_HTTPS_TOKEN_FILE` | `/run/secrets/git_https_token` | `git_https_token` |
| `MEMINI_GIT_SSH_KEY_PATH` | `/run/secrets/git_ssh_key` | `git_ssh_key_path` |
| `MEMINI_GIT_SSH_KNOWN_HOSTS_PATH` | `/run/secrets/git_ssh_known_hosts` | `git_ssh_known_hosts_path` |
| `MEMINI_CREDENTIAL_KEY_FILE` | `/run/secrets/credential_key` | `credential_key` |
| `MEMINI_CREDENTIAL_OLD_KEYS_FILE` | `/run/secrets/credential_old_keys` | `credential_old_keys` |
| `MEMINI_LLM_LOCAL_API_KEY_FILE` | `/run/secrets/llm_local_api_key` | `llm_local_api_key` |
| `MEMINI_LLM_CLOUD_API_KEY_FILE` | `/run/secrets/llm_cloud_api_key` | `llm_cloud_api_key` |
| `MEMINI_METRICS_TOKEN_FILE` | `/run/secrets/metrics_token` | `metrics_token` |

### Local repositories

A local repository is a source only under `git_local_roots`, and the container sees no directory of the host until it is mounted: mount the directory that holds the repositories, read-only, and list its path in the container in `MEMINI_GIT_LOCAL_ROOTS`. `compose.yaml` has an example under `volumes`.
