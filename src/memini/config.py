"""Service configuration: one typed, frozen settings object, validated once at start.

Call :func:`load_settings` at start. It returns a :class:`Settings` or raises :class:`ConfigError`,
whose message names every missing or invalid key and never holds a value.

Where values come from, highest precedence first:

1. The environment. Setting ``http_port`` is read from ``MEMINI_HTTP_PORT``: the prefix ``MEMINI_``
   and the setting name in upper case. A variable set to the empty string counts as not set. Other
   ``MEMINI_*`` variables are ignored.
2. A TOML file, if ``MEMINI_CONFIG_FILE`` holds its path. Keys are setting names at the top level,
   for example ``http_port = 6364``. An unknown key is an error, so a typo does not go unnoticed.
3. The defaults declared on :class:`Settings`.

Secrets are the settings typed ``SecretStr``; ``database_url`` is one, as it carries a password. A
secret comes from the environment only, in one of two forms:

- ``MEMINI_DATABASE_URL``: the value itself;
- ``MEMINI_DATABASE_URL_FILE``: the path to a file that holds the value, as Docker and compose
  secrets provide it. Trailing line breaks in the file are dropped.

Setting both forms is an error, and so is a secret in the TOML file: the file holds nothing secret
and can be shared. A secret value never appears in ``repr``, ``str``, logs or error text; read it
with ``settings.database_url.get_secret_value()`` at the point of use.

For the development compose stack, where Postgres listens on 127.0.0.1:55432::

    MEMINI_DATABASE_URL=postgresql://memini:memini@127.0.0.1:55432/memini

``principal`` (``MEMINI_PRINCIPAL``, default ``owner``) names the principal the operator commands
act as: ``memini bootstrap`` creates its first admin token, so it owns what is made with that
token, and it is created on first use. v1 has one user, so the default fits; set it only to give
that principal another name, and keep it the same afterwards. The CLI's client commands do not read
these settings: see :mod:`memini.cli.client`.

Embeddings (the ``embedding_*`` settings, :mod:`memini.embeddings`) are off until
``embedding_provider`` is set: the service starts without them, and indexing that needs vectors
refuses with a message naming the setting. ``openai_compatible`` sends chunk text to the API at
``embedding_url`` (Ollama, llama.cpp, vLLM or a cloud service): which endpoint, on this machine or
not, is the user's explicit choice (principle 8). ``local`` runs an ONNX model in the process; it
needs the optional dependencies ``memini[local-embeddings]`` and pins the model files by
``embedding_digest``, which is verified before they are loaded. A setting of the provider not
chosen, or any ``embedding_*`` setting without a provider, is an error. The index fingerprint
(:mod:`memini.index`) covers provider, model, digest, dimension, pooling, token limit and document
prefix: a change of any of them blocks search until ``memini reindex`` rebuilds the index.

The reranker (the ``search_reranker*`` settings, :mod:`memini.rerank`) is ``none`` by default
and ``local`` in the image, which carries its runtime and files. ``local`` reorders the best
``search_rerank_depth`` hits of a ranked search with a cross-encoder run in the process. It needs
``memini[local-embeddings]`` too, and its files are pinned by ``search_reranker_digest`` and
fetched at ``search_reranker_revision`` when they are not in ``search_reranker_model_dir``. The
defaults name the model the eval measured; another model or model file needs its own digest (and
revision). A ``search_reranker_*`` setting with ``search_reranker`` ``none`` is an error, but for
``search_reranker_model_dir``, which the image sets either way. Nothing of the reranker is part of
the index fingerprint.

The ``llm_*`` settings declare at most two LLM providers (:mod:`memini.llm`), each an
OpenAI-compatible chat API: ``llm_local_*`` one you trust with the content of every source (Ollama,
llama.cpp, vLLM on your own machine), ``llm_cloud_*`` one you trust only with sources whose
``llm_policy`` is ``cloud``, in workspaces that opted in (principle 8). The slot an endpoint is set
in is the declaration; it is never inferred from the URL. ``llm_cloud_url`` must use https. Each
slot needs its URL and model together; a setting of a slot without its URL is an error. No
provider is configured by default, and then nothing is extracted.

The default model is Qwen3-Embedding 0.6B as Ollama serves it (docs/requirements.md, open question
2). With ``embedding_provider = openai_compatible`` and nothing else, the settings are:

- ``embedding_url``: :data:`DEFAULT_EMBEDDING_URL`, Ollama on this machine;
- ``embedding_model``: :data:`DEFAULT_EMBEDDING_MODEL`;
- and, while the model is that one, ``embedding_dimension``, ``embedding_digest`` and
  ``embedding_query_prefix`` from :data:`DEFAULT_MODEL_SETTINGS`: 1024, the Ollama digest the eval
  measured, and the instruction the model expects before a query.

A value that is set wins over its default. Another model gets no dimension, digest or prefix from
here: it needs its own ``embedding_dimension``. Under compose, ``embedding_url`` is set with the
provider to the host's Ollama, as inside a container 127.0.0.1 is the container
(.env.memini.example).

The ``git_*`` settings configure the git connector (:mod:`memini.git`): where the mirrors live, the
limits, which transports a source may use and the credentials for remotes. A list setting is a
TOML array in the config file, or comma-separated in the environment
(``MEMINI_GIT_LOCAL_ROOTS=/srv/repos,/home/me/src``). A size is a number of bytes, or a number with
a binary unit: ``512M``, ``5G``, ``1GiB``.

The ``credential_*`` settings hold the keys of the credential store (:mod:`memini.credentials`),
where the API keys and OAuth tokens of sources are kept, encrypted. ``credential_key`` is 32 random
bytes in base64 (``openssl rand -base64 32``). The service starts without it; storing or reading a
credential then fails with a message that names the setting. ``credential_old_keys``,
comma-separated, are keys still accepted for reading while ``memini credentials rotate-key``
re-encrypts the store with ``credential_key``. Both are secrets.

The ``mcp_*`` settings bound the service executor of MCP recipes (:mod:`memini.executors`): the
endpoints an mcp source may name (hosts or URL prefixes), the internal networks they may resolve to
all the same, plain http, the executables a stdio source may run, and how often one call is tried.
Every list is empty by default, so no mcp source reaches anything until the operator lists it.
``mcp_enabled`` is not one of them: it switches the service's own MCP endpoint
(:mod:`memini.api.mcp`), on by default.

``metrics_token``, a secret, turns on ``GET /metrics`` (:mod:`memini.api.metrics`): a scraper sends
it as its bearer token. ``log_slow_statement_threshold`` is the time after which a database
statement of the service is logged as slow (:mod:`memini.db.statements`).
"""

import base64
import binascii
import ipaddress
import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final, Literal, cast, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "CONFIG_FILE_VAR",
    "CREDENTIAL_KEY_BYTES",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_EMBEDDING_URL",
    "DEFAULT_MODEL_SETTINGS",
    "DEFAULT_RERANKER_DIGEST",
    "DEFAULT_RERANKER_MODEL",
    "DEFAULT_RERANKER_MODEL_FILE",
    "DEFAULT_RERANKER_REVISION",
    "DEFAULT_RERANK_DEPTH",
    "EMBEDDING_MAX_DIMENSION",
    "ENV_PREFIX",
    "HANDLE_REGEX",
    "ConfigError",
    "EmbeddingPooling",
    "EmbeddingProvider",
    "LlmStructuredOutput",
    "LogLevel",
    "RerankerProvider",
    "Settings",
    "decode_credential_key",
    "environment_variables",
    "load_settings",
]

ENV_PREFIX: Final = "MEMINI_"
CONFIG_FILE_VAR: Final = "MEMINI_CONFIG_FILE"
_FILE_SUFFIX: Final = "_FILE"

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

EmbeddingProvider = Literal["openai_compatible", "local"]
"""Where vectors come from: an OpenAI-compatible HTTP API, or an ONNX model in the process."""

EmbeddingPooling = Literal["mean", "cls"]
"""How the local provider makes one vector of a text's token vectors: their mean, or the vector of
the first token."""

LlmStructuredOutput = Literal["json_schema", "json_object", "none"]
"""How an LLM endpoint is asked for JSON: ``response_format`` with the JSON schema, with
``json_object``, or not at all (the schema goes into the prompt). The reply is parsed and validated
against the schema in every case."""

RerankerProvider = Literal["local", "none"]
"""Where reranking runs: ``local``, a cross-encoder in the process (ONNX); ``none``, nowhere."""

CandidateRetriever = Literal["lexical", "hybrid"]
"""How the candidates of a supersession check are found: by the words of the statements, or by
their words and their vectors fused (``hybrid``, which needs an embedding provider and is
``lexical`` without one)."""

EMBEDDING_MAX_DIMENSION: Final = 16000
"""The largest dimension a pgvector ``vector`` column holds."""

DEFAULT_RERANK_DEPTH: Final = 3
"""How many fused hits a reranker reorders (``search_rerank_depth``). Also the default of
:attr:`memini.search.SearchOptions.rerank_depth`. 3 rather than 5: nearly the same gain on the
eval for three fifths of the time, which keeps a search within its budget on 2 to 4 CPUs
(docs/requirements.md, section 9, Reranker)."""

DEFAULT_RERANKER_MODEL: Final = "n24q02m/Qwen3-Reranker-0.6B-ONNX"
"""Qwen3-Reranker 0.6B as an ONNX export with a two-logit head (docs/requirements.md, section 9,
Reranker)."""
DEFAULT_RERANKER_MODEL_FILE: Final = "onnx/model_yesno_quantized.onnx"
DEFAULT_RERANKER_DIGEST: Final = (
    "sha256:eaa6edfadd2440e4f80851b3ff7701f26e8ec6aacaa2fedcee8ce86afd09afea"
)
"""The digest of the default reranker's model file and ``tokenizer.json`` at its revision."""
DEFAULT_RERANKER_REVISION: Final = "342a8a54de23bba03eb8cce5f5bef503edf18315"

DEFAULT_EMBEDDING_URL: Final = "http://127.0.0.1:11434/v1"
"""Where ``openai_compatible`` sends text when ``embedding_url`` is not set: Ollama on this
machine, at its default port."""

DEFAULT_EMBEDDING_MODEL: Final = "qwen3-embedding:0.6b"
"""The model ``openai_compatible`` asks for when ``embedding_model`` is not set: Qwen3-Embedding
0.6B by its Ollama name (docs/requirements.md, open question 2). ``ollama pull`` it first."""

DEFAULT_MODEL_SETTINGS: Final[Mapping[str, object]] = {
    "embedding_dimension": 1024,
    # The digest of the Ollama build the eval baseline was measured with (Q8_0), as
    # `ollama list` and /api/tags show it. Recorded in the index fingerprint, not checked.
    "embedding_digest": "ac6da0dfba84a81fdbfbaf330198c33cd77c4cdfc53e8bc50eb581914a15621d",
    # Qwen3-Embedding embeds a query after an instruction, in the form "Instruct: {task}\nQuery:",
    # and a document without one. This task scored best of the variants measured on the code
    # suite (docs/requirements.md, open question 2). Not part of the fingerprint: changing it
    # needs no reindex.
    "embedding_query_prefix": (
        "Instruct: Given a search query, retrieve the relevant code or documentation that can help"
        " answer the query\nQuery:"
    ),
}
"""The settings :data:`DEFAULT_EMBEDDING_MODEL` gets when they are not set."""

HANDLE_REGEX: Final = r"^[a-z0-9][a-z0-9_-]{0,62}$"
"""A valid name of a principal, as :data:`memini.workspaces.HANDLE_PATTERN` and the database check
it. Repeated here so that this module imports no database code; a test keeps the two equal."""


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


# --- Git connector: value types ------------------------------------------------------------------

_BYTE_SIZE: Final = re.compile(r"(?i)\s*(\d+)\s*(?:([kmgt])(?:i?b)?|b)?\s*")
_BYTE_UNITS: Final = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
_HOSTNAME: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def _byte_size(value: object) -> object:
    """``1048576``, ``512M``, ``5G``, ``1GiB`` as a number of bytes; anything else as it is, for
    the integer check to refuse."""
    if isinstance(value, str) and (match := _BYTE_SIZE.fullmatch(value)):
        number, unit = match.groups()
        return int(number) * (_BYTE_UNITS[unit.lower()] if unit else 1)
    return value


def _comma_list(value: object) -> object:
    """A comma-separated string as a tuple of its non-empty, stripped items; a list as it is."""
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


def _absolute(path: Path | None) -> Path | None:
    if path is not None and not path.is_absolute():
        raise ValueError("not an absolute path")
    return path


def _hostname(value: str) -> str:
    host = value.lower()
    if len(host) > 253 or _HOSTNAME.fullmatch(host) is None:
        raise ValueError("not a host name")
    return host


def _git_origin(value: str, *, proxy: bool = False) -> str:
    from memini.executors.egress import EgressRefused, parse_endpoint_url

    try:
        target = parse_endpoint_url(value, allow_http=proxy)
    except EgressRefused:
        raise ValueError(
            "expected a Git origin without credentials, path, query or fragment"
        ) from None
    if target.path != "/":
        raise ValueError("a Git origin has no path")
    host = f"[{target.host}]" if ":" in target.host else target.host
    return f"{target.scheme}://{host}:{target.port}"


def _git_proxy(value: str) -> str:
    return _git_origin(value, proxy=True)


def _single_line(secret: SecretStr | None) -> SecretStr | None:
    if secret is not None and any(
        c.isspace() or not c.isprintable() for c in secret.get_secret_value()
    ):
        raise ValueError("holds whitespace or control characters")
    return secret


type _ByteSize = Annotated[int, BeforeValidator(_byte_size)]
type _AbsolutePath = Annotated[Path, AfterValidator(_absolute)]
type _Hostname = Annotated[str, AfterValidator(_hostname)]


# --- Credential store: value types ---------------------------------------------------------------

CREDENTIAL_KEY_BYTES: Final = 32
"""The length of a credential store key: AES-256."""


def decode_credential_key(text: str) -> bytes:
    """The bytes of a credential store key written in base64, in the standard or the URL-safe
    alphabet, with or without padding.

    Raises:
        ValueError: ``text`` is not base64 of :data:`CREDENTIAL_KEY_BYTES` bytes. The message never
            holds the text.
    """
    padded = text + "=" * (-len(text) % 4)
    try:
        key = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except binascii.Error:
        raise ValueError("not base64") from None
    if len(key) != CREDENTIAL_KEY_BYTES:
        raise ValueError(f"not {CREDENTIAL_KEY_BYTES} bytes")
    return key


def _credential_key(secret: SecretStr) -> SecretStr:
    decode_credential_key(secret.get_secret_value())
    return secret


_CREDENTIAL_KEY_DESCRIPTION: Final = (
    "32 random bytes in base64, e.g. from `openssl rand -base64 32`"
)


# --- MCP service executor: value types -----------------------------------------------------------


def _endpoint_rule(value: str) -> str:
    # The rule's reader, so the settings refuse what the executor could not read. Imported here:
    # memini.executors.egress reads these settings.
    from memini.executors.egress import EgressRefused, EndpointRule

    try:
        EndpointRule.parse(value)
    except (ValueError, EgressRefused) as error:
        raise ValueError(str(error)) from None
    return value.strip()


def _network(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise ValueError("not a network in CIDR notation, such as 10.0.0.0/8") from None


class StdioInvocation(BaseModel):
    """One operator-approved executable, argument vector and credential destination."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: _AbsolutePath
    args: tuple[str, ...] = ()
    credential_env: str | None = None


def _invocation_list(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


class Settings(BaseModel):
    """Validated service settings. Build it with :func:`load_settings`."""

    # hide_input_in_errors keeps values out of a ValidationError's text, should one ever escape.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    database_url: SecretStr = Field(min_length=1, description="a Postgres connection URL")
    http_host: str = Field(
        default="127.0.0.1",
        min_length=1,
        description="the address the HTTP server binds to",
    )
    http_port: int = Field(default=6364, ge=1, le=65535, description="a TCP port from 1 to 65535")
    # The service's own MCP endpoint (memini.api.mcp), not the executor of MCP recipes below
    # (mcp_allowed_endpoints and the others). Off: /mcp answers 404 mcp_disabled, the API stays.
    mcp_enabled: bool = Field(
        default=True,
        description="true or false: serve the MCP endpoint at /mcp",
    )
    log_level: Annotated[LogLevel, BeforeValidator(_upper)] = Field(
        default="INFO",
        description="one of CRITICAL, ERROR, WARNING, INFO, DEBUG",
    )
    principal: str = Field(
        default="owner",
        pattern=HANDLE_REGEX,
        description="1 to 63 lowercase letters, digits, '-' or '_', starting with a letter"
        " or digit",
    )

    # --- Embeddings (memini.embeddings). Rules across these fields: _embedding_problems. ---------
    embedding_provider: EmbeddingProvider | None = Field(
        default=None, description="openai_compatible or local"
    )
    embedding_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="a model name; for local, a Hugging Face repository such as org/name",
    )
    embedding_dimension: int | None = Field(
        default=None,
        ge=1,
        le=EMBEDDING_MAX_DIMENSION,
        description=f"the model's vector size, an integer from 1 to {EMBEDDING_MAX_DIMENSION}",
    )
    embedding_digest: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="for local, sha256: and 64 lowercase hex digits; for openai_compatible, the"
        " version of the served model, such as its Ollama digest",
    )
    embedding_batch_size: int = Field(
        default=32, ge=1, le=2048, description="texts per batch, an integer from 1 to 2048"
    )
    embedding_timeout: float = Field(
        default=60.0, gt=0, le=3600, description="seconds per request, more than 0, at most 3600"
    )
    embedding_query_prefix: str = Field(
        default="", max_length=200, description="at most 200 characters, e.g. 'query: '"
    )
    embedding_document_prefix: str = Field(
        default="", max_length=200, description="at most 200 characters, e.g. 'passage: '"
    )
    # openai_compatible only
    embedding_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^https?://[^\s/?#]+(/[^\s?#]*)?$",
        description="the base URL of an OpenAI-compatible API, e.g. http://127.0.0.1:11434/v1"
        " (the default)",
    )
    embedding_api_key: SecretStr | None = Field(default=None, min_length=1)
    # local only
    embedding_model_dir: Path | None = Field(
        default=None, description="a directory for the model files; created if missing"
    )
    embedding_model_file: str = Field(
        default="onnx/model.onnx",
        max_length=200,
        pattern=r"^([A-Za-z0-9_][A-Za-z0-9._-]*/)*[A-Za-z0-9_][A-Za-z0-9._-]*\.onnx$",
        description="the path of the ONNX file in the model repository, e.g. onnx/model.onnx",
    )
    embedding_revision: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description="a commit of the model repository, 40 lowercase hex digits",
    )
    embedding_pooling: EmbeddingPooling = Field(default="mean", description="mean or cls")
    embedding_max_tokens: int = Field(
        default=512, ge=1, le=32768, description="an integer from 1 to 32768"
    )

    # --- Git connector (memini.git) --------------------------------------------------------------
    # The service keeps a bare mirror of every git source and reads code from it. Remote sources
    # use https or ssh; plain http and local paths are refused unless allowed here. Credentials
    # are global for now, one https token and one ssh key; per-source credentials come later.
    git_mirror_dir: Annotated[Path | None, AfterValidator(_absolute)] = Field(
        default=None,
        description="an absolute directory path; unset: $XDG_DATA_HOME/memini/mirrors, or"
        " ~/.local/share/memini/mirrors (the image sets /var/lib/memini/mirrors)",
    )
    git_fetch_timeout: int = Field(
        default=600,
        ge=1,
        le=86400,
        description="seconds a clone or fetch may take, 1 to 86400",
    )
    git_max_mirror_size: _ByteSize = Field(
        default=5 * 1024**3,
        ge=1,
        description="the largest size of one mirror on disk: bytes, or a number with K, M, G or T",
    )
    git_max_blob_size: _ByteSize = Field(
        default=1024**2,
        ge=1,
        description="the largest file read from a mirror: bytes, or a number with K, M, G or T",
    )
    git_allow_http: bool = Field(
        default=False,
        description="true or false: allow plain http:// remotes (no TLS)",
    )
    git_egress_proxy: Annotated[str, AfterValidator(_git_proxy)] | None = Field(
        default=None,
        description="HTTP(S) CONNECT proxy origin for restricted Git egress; "
        "the proxy must enforce destination address and port ACLs; unset: legacy transports",
    )
    git_allowed_origins: Annotated[
        tuple[Annotated[str, AfterValidator(_git_origin)], ...], BeforeValidator(_comma_list)
    ] = Field(
        default=(),
        description="exact HTTPS origins permitted through git_egress_proxy, "
        "comma-separated; empty with a proxy: deny network Git; requires git_egress_proxy",
    )
    git_local_roots: Annotated[tuple[_AbsolutePath, ...], BeforeValidator(_comma_list)] = Field(
        default=(),
        description="absolute directory paths, comma-separated, under which local repositories"
        " may be sources; empty: no local sources",
    )
    git_https_token: Annotated[SecretStr | None, AfterValidator(_single_line)] = Field(
        default=None,
        description="a token for https remotes, without whitespace",
    )
    git_https_username: str = Field(
        default="x-access-token",
        pattern=r"^[!-~]{1,256}$",
        description="the user name sent with git_https_token: printable ASCII, no spaces",
    )
    git_https_token_hosts: Annotated[tuple[_Hostname, ...], BeforeValidator(_comma_list)] = Field(
        default=(),
        validate_default=True,
        description="host names, comma-separated, that git_https_token is sent to; required"
        " when the token is set",
    )
    git_ssh_key_path: Annotated[Path | None, AfterValidator(_absolute)] = Field(
        default=None,
        description="the absolute path of the private key for ssh remotes",
    )
    git_ssh_known_hosts_path: Annotated[Path | None, AfterValidator(_absolute)] = Field(
        default=None,
        description="the absolute path of the known_hosts file that ssh remotes are checked"
        " against",
    )
    # History is read from the mirrors on demand (list_commits, memini.query.commits) and never
    # stored: what one call returns, what it reads from one repository, and how long it may run.
    git_log_max_commits: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="the most commits one list_commits call returns, an integer from 1 to 1000",
    )
    git_log_max_walk: int = Field(
        default=2000,
        ge=1,
        le=100000,
        description="the most commits one list_commits call reads from the history of one"
        " repository, an integer from 1 to 100000",
    )
    git_log_timeout: float = Field(
        default=10.0,
        gt=0,
        le=600,
        description="seconds the history walks of one list_commits call may take, more than 0, at"
        " most 600",
    )

    # --- Credential store (memini.credentials) ----------------------------------------------------
    # The API keys and OAuth tokens of sources are stored encrypted with credential_key
    # (AES-256-GCM). Without it the service starts, and the credential store refuses to work.
    # credential_old_keys are accepted for reading only, while a rotation runs. The annotations
    # are written out, not a `type` alias: a setting is a secret when its annotation names
    # SecretStr (_mentions_secret), which an alias would hide.
    credential_key: Annotated[SecretStr, AfterValidator(_credential_key)] | None = Field(
        default=None, description=_CREDENTIAL_KEY_DESCRIPTION
    )
    credential_old_keys: Annotated[
        tuple[Annotated[SecretStr, AfterValidator(_credential_key)], ...],
        BeforeValidator(_comma_list),
    ] = Field(
        default=(),
        description="keys still read during a rotation, comma-separated, each "
        + _CREDENTIAL_KEY_DESCRIPTION
        + "; only together with credential_key",
    )

    # --- MCP service executor (memini.executors) --------------------------------------------------
    # mcp sources are MCP servers the service syncs on its own. It connects only to the endpoints
    # listed here, to an internal address only in a network listed here, and runs as stdio servers
    # only the executables listed here (memini.executors.egress).
    mcp_allowed_endpoints: Annotated[
        tuple[Annotated[str, AfterValidator(_endpoint_rule)], ...], BeforeValidator(_comma_list)
    ] = Field(
        default=(),
        description="hosts (mcp.example.com, host:port, *.example.com) or URL prefixes"
        " (https://mcp.example.com/v1), comma-separated, that mcp sources may connect to; empty:"
        " none",
    )
    mcp_allowed_networks: Annotated[
        tuple[Annotated[str, AfterValidator(_network)], ...], BeforeValidator(_comma_list)
    ] = Field(
        default=(),
        description="networks in CIDR notation, comma-separated, of internal addresses (loopback,"
        " private, link-local) that MCP endpoints may resolve to all the same; empty: none",
    )
    mcp_allow_http: bool = Field(
        default=False,
        description="true or false: allow plain http:// MCP endpoints (no TLS)",
    )
    mcp_allowed_commands: Annotated[tuple[_AbsolutePath, ...], BeforeValidator(_comma_list)] = (
        Field(
            default=(),
            description="absolute paths, comma-separated, of the executables mcp sources may run as"
            " stdio servers; empty: none",
        )
    )
    mcp_allowed_invocations: Annotated[
        tuple[StdioInvocation, ...], BeforeValidator(_invocation_list)
    ] = Field(
        default=(),
        description="approved stdio invocations with command, args and optional credential_env;"
        " a JSON array in the environment or an array of tables in TOML",
    )
    mcp_call_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="attempts of one call of an mcp sync, the first included, 1 to 10",
    )

    # --- Search (memini.search) -------------------------------------------------------------------
    # Limits of one search: how long a statement may run (a regular expression can backtrack
    # without end), how many candidates each retriever of a hybrid search contributes, and the k
    # of reciprocal rank fusion and the weight of each retriever's ranks in it.
    search_timeout: float = Field(
        default=5.0,
        gt=0,
        le=600,
        description="seconds one search statement may run, more than 0, at most 600",
    )
    search_candidates: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="candidates per retriever of a hybrid search, an integer from 1 to 1000",
    )
    search_rrf_k: int = Field(
        default=1,
        ge=1,
        le=1000,
        description="the k of reciprocal rank fusion, an integer from 1 to 1000",
    )
    search_dense_weight: float = Field(
        default=3.0,
        gt=0,
        le=100,
        description="the weight of the dense retriever's ranks in the fusion, more than 0, at"
        " most 100",
    )
    search_lexical_weight: float = Field(
        default=1.0,
        gt=0,
        le=100,
        description="the weight of the lexical retriever's ranks in the fusion, more than 0, at"
        " most 100",
    )

    # --- Reranker (memini.rerank). Rules across these fields: _reranker_problems. ---------------
    # none by default: outside the image it needs the optional runtime and a 610 MB download. The
    # image, which carries both, sets local (Dockerfile). The other defaults are what the
    # retrieval eval measured and chose (docs/requirements.md, section 9, Reranker). `local` runs
    # a cross-encoder in the process, with the optional dependencies memini[local-embeddings]; its
    # files are pinned by search_reranker_digest like those of the local embedding provider.
    # search_rerank_depth is read by any reranker, also the one `memini eval --reranker` measures.
    search_reranker: RerankerProvider = Field(
        default="none",
        description="local: a cross-encoder run in the process; none: no reranking",
    )
    search_rerank_depth: int = Field(
        default=DEFAULT_RERANK_DEPTH,
        ge=1,
        le=100,
        description="how many of the best fused hits the reranker reorders, an integer from 1 to"
        " 100",
    )
    search_reranker_model: str = Field(
        default=DEFAULT_RERANKER_MODEL,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="a Hugging Face repository such as org/name",
    )
    search_reranker_model_file: str = Field(
        default=DEFAULT_RERANKER_MODEL_FILE,
        max_length=200,
        pattern=r"^([A-Za-z0-9_][A-Za-z0-9._-]*/)*[A-Za-z0-9_][A-Za-z0-9._-]*\.onnx$",
        description="the path of the ONNX file in the model repository",
    )
    search_reranker_digest: str = Field(
        default=DEFAULT_RERANKER_DIGEST,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="sha256: and 64 lowercase hex digits, the digest of the model files",
    )
    search_reranker_revision: str = Field(
        default=DEFAULT_RERANKER_REVISION,
        pattern=r"^[0-9a-f]{40}$",
        description="a commit of the model repository, 40 lowercase hex digits",
    )
    search_reranker_model_dir: Annotated[Path | None, AfterValidator(_absolute)] = Field(
        default=None,
        description="an absolute directory path for the model files, created if missing; unset:"
        " $XDG_CACHE_HOME/memini/models, or ~/.cache/memini/models",
    )
    search_reranker_max_tokens: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description="the most tokens of one query and hit put to the model, 64 to 8192",
    )
    search_reranker_timeout: float = Field(
        default=5.0,
        gt=0,
        le=600,
        description="seconds the reranking of one search may take, more than 0, at most 600",
    )
    search_reranker_threads: int = Field(
        default=0,
        ge=0,
        le=256,
        description="threads of one model run, 0 to 256; 0: as many as the CPUs or a CPU limit of"
        " the container allow, at most 4",
    )

    # --- Ingest (memini.ingest) -------------------------------------------------------------------
    # Limits of one batch of records sent to the ingest API: the request body, the records in it,
    # and the payload of one record in its stored form. A body over the limit is refused by its
    # Content-Length before it is read, or as soon as more of it arrives.
    ingest_max_body_size: _ByteSize = Field(
        default=16 * 1024**2,
        ge=1024,
        le=1024**3,
        description="the largest request body of an ingest batch or of an agent run's raw"
        " response, 1K to 1G: bytes, or a number with K, M or G",
    )
    ingest_max_records: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="the most records and tombstones in one ingest batch, 1 to 100000",
    )
    ingest_max_payload_size: _ByteSize = Field(
        default=1024**2,
        ge=1024,
        le=1024**3,
        description="the largest payload of one record as stored, 1K to 1G: bytes, or a number"
        " with K, M or G",
    )

    # --- Retention (memini.removal) ---------------------------------------------------------------
    # A source with retention_days loses its expired records: the scheduler enqueues a retention
    # job of it every retention_interval, which deletes them retention_batch_size at a time, one
    # transaction each. A git sync ends with the same pass over its source.
    retention_interval: int = Field(
        default=3600,
        ge=60,
        le=7 * 86400,
        description="seconds between two retention passes over a source that has retention_days,"
        " 60 to 604800",
    )
    retention_batch_size: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description="records one transaction of a retention pass deletes at most, 1 to 100000",
    )

    # --- HTTP request limits (memini.api.limits, memini.db.limits) -------------------------------
    # Limits of every HTTP and MCP request. The body: refused by its Content-Length before it is
    # read, or as soon as more of it arrives (the ingest route and the route of an agent run's raw
    # responses have their own, ingest_max_body_size).
    # The time of each database statement and of an idle transaction a request runs (search sets
    # its own statement limit, search_timeout); 0 leaves the database's default. Rate limits: per
    # token, a budget of reads and one of writes (an MCP call of a write tool is a write); per
    # client address, one for requests whose token is not known to be valid. Each budget is a
    # minute's worth of requests, refilled steadily.
    http_max_body_size: _ByteSize = Field(
        default=1024**2,
        ge=1024,
        le=1024**3,
        description="the largest request body outside the ingest and raw response routes, 1K to"
        " 1G: bytes, or a number with K, M or G",
    )
    http_statement_timeout: float = Field(
        default=30.0,
        ge=0,
        le=86400,
        description="seconds one database statement of a request may run, 0 to 86400; 0: the"
        " database's default",
    )
    http_idle_in_transaction_timeout: float = Field(
        default=300.0,
        ge=0,
        le=86400,
        description="seconds a request's transaction may stay idle, 0 to 86400; 0: the database's"
        " default",
    )
    http_rate_limit: bool = Field(
        default=True,
        description="true or false: limit requests per token and per client address",
    )
    http_rate_limit_reads: int = Field(
        default=600,
        ge=1,
        le=1_000_000,
        description="reads (GET, and every MCP request but a call of a write tool) per minute"
        " per token, 1 to 1000000",
    )
    http_rate_limit_writes: int = Field(
        default=120,
        ge=1,
        le=1_000_000,
        description="writes (POST, PUT, PATCH, DELETE, and MCP calls of write tools) per minute"
        " per token, 1 to 1000000",
    )
    http_rate_limit_unauthenticated: int = Field(
        default=60,
        ge=1,
        le=1_000_000,
        description="requests per minute per client address whose token is not known to be"
        " valid, 1 to 1000000",
    )

    # --- LLM providers (memini.llm). Rules across these fields: _llm_problems. -------------------
    # Two OpenAI-compatible chat APIs, declared by the slot they are set in: the local one may
    # read content of sources whose llm_policy is local or cloud, the cloud one only content of
    # sources whose llm_policy is cloud, in workspaces that opted in. The gate (memini.llm.gate)
    # enforces it; the declaration is the user's, never inferred from a host name.
    llm_local_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^https?://[^\s/?#]+(/[^\s?#]*)?$",
        description="the base URL of an OpenAI-compatible API you trust with every source's"
        " content, e.g. http://127.0.0.1:11434/v1",
    )
    llm_local_model: str | None = Field(
        default=None, min_length=1, max_length=200, description="the model the local API serves"
    )
    llm_local_api_key: Annotated[SecretStr | None, AfterValidator(_single_line)] = Field(
        default=None, min_length=1, description="a key for the local API, without whitespace"
    )
    llm_local_structured_output: LlmStructuredOutput = Field(
        default="json_schema",
        description="json_schema, json_object or none: how the local API is asked for JSON",
    )
    llm_cloud_url: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^https://[^\s/?#]+(/[^\s?#]*)?$",
        description="the https:// base URL of an OpenAI-compatible API that may read sources"
        " whose llm_policy is cloud, in workspaces that opted in",
    )
    llm_cloud_model: str | None = Field(
        default=None, min_length=1, max_length=200, description="the model the cloud API serves"
    )
    llm_cloud_api_key: Annotated[SecretStr | None, AfterValidator(_single_line)] = Field(
        default=None, min_length=1, description="a key for the cloud API, without whitespace"
    )
    llm_cloud_structured_output: LlmStructuredOutput = Field(
        default="json_schema",
        description="json_schema, json_object or none: how the cloud API is asked for JSON",
    )
    llm_timeout: float = Field(
        default=300.0,
        gt=0,
        le=3600,
        description="seconds per LLM request, more than 0, at most 3600",
    )

    # --- Decision extraction (memini.extraction.job). Runs only with an LLM provider. -----------
    # Each new or changed thread or meeting gets an extract job: one LLM call extracts its
    # decisions and open questions, and one call per item checks it against the most similar
    # active items of the workspace, found by the words of the statements and, with `hybrid` and
    # an embedding provider, by their vectors too (memini.extraction.candidates).
    extraction_candidates: int = Field(
        default=8,
        ge=1,
        le=50,
        description="active items, 1 to 50, that a new item is checked against for duplicates,"
        " replacements and answers",
    )
    extraction_candidate_retriever: CandidateRetriever = Field(
        default="hybrid",
        description="lexical or hybrid: how those items are found, by the words of the statements"
        " or by their words and their vectors",
    )

    # --- Observability (memini.metrics, memini.api.metrics, memini.db.statements) ----------------
    # GET /metrics serves Prometheus metrics, counts only, to a scraper that sends metrics_token
    # as its bearer token; without it there is no such route. A Memini token is not accepted
    # there: one that may read metrics could read content too. Every statement on the service's
    # pool is timed; one over log_slow_statement_threshold is logged with its duration and a
    # fingerprint of its text, never its parameters.
    metrics_token: Annotated[SecretStr | None, AfterValidator(_single_line)] = Field(
        default=None,
        min_length=32,
        max_length=512,
        description="a random secret of at least 32 characters, without whitespace, e.g. from"
        " `openssl rand -hex 32`",
    )
    log_slow_statement_threshold: float = Field(
        default=1.0,
        ge=0,
        le=86400,
        description="seconds a database statement may take before it is logged as slow, 0 to"
        " 86400; 0: no log",
    )

    @model_validator(mode="before")
    @classmethod
    def _default_embedding_model(cls, data: Any) -> Any:
        """Fill in the default model of ``openai_compatible`` and its settings (module docstring).

        Runs before validation, on the merged input: a value that is set, even to something
        invalid, is left for validation to judge."""
        if not isinstance(data, Mapping):
            return data
        values = dict(cast(Mapping[str, object], data))
        if values.get("embedding_provider") != "openai_compatible":
            return values
        if values.get("embedding_url") is None:
            values["embedding_url"] = DEFAULT_EMBEDDING_URL
        if values.get("embedding_model") is None:
            values["embedding_model"] = DEFAULT_EMBEDDING_MODEL
        if values["embedding_model"] == DEFAULT_EMBEDDING_MODEL:
            for name, value in DEFAULT_MODEL_SETTINGS.items():
                if values.get(name) is None:
                    values[name] = value
        return values

    @field_validator("git_allowed_origins")
    @classmethod
    def _origins_need_proxy(cls, origins: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        if origins and info.data.get("git_egress_proxy") is None:
            raise ValueError("git_allowed_origins requires git_egress_proxy")
        return origins

    @field_validator("git_https_token_hosts")
    @classmethod
    def _token_has_hosts(cls, hosts: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        # A token without hosts would go to whatever https host a source names.
        if info.data.get("git_https_token") is not None and not hosts:
            raise ValueError("git_https_token is set without the hosts it may be sent to")
        return hosts

    @field_validator("credential_old_keys")
    @classmethod
    def _old_keys_need_a_key(
        cls, keys: tuple[SecretStr, ...], info: ValidationInfo
    ) -> tuple[SecretStr, ...]:
        # An old key is only read from; without a current key nothing could be rotated to. Not
        # checked when credential_key itself was refused: that problem is reported already.
        if keys and "credential_key" in info.data and info.data["credential_key"] is None:
            raise ValueError("credential_old_keys is set without credential_key")
        return keys


class ConfigError(Exception):
    """The configuration is missing or invalid.

    ``problems`` holds one line per problem, each starting with the key it concerns. The text names
    keys and kinds of problems only, never values. The CLI prints it and exits non-zero.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        listing = "\n".join(f"  {problem}" for problem in self.problems)
        super().__init__(f"invalid configuration:\n{listing}")


def environment_variables() -> tuple[str, ...]:
    """Every environment variable :func:`load_settings` reads, in field order."""
    names = [CONFIG_FILE_VAR]
    for name in Settings.model_fields:
        names.append(_env_var(name))
        if _is_secret(name):
            names.append(_env_var(name) + _FILE_SUFFIX)
    return tuple(names)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read, merge and validate the settings.

    ``environ`` defaults to ``os.environ``; tests pass a mapping of their own. The config file
    path, if any, is read from ``MEMINI_CONFIG_FILE`` in that mapping.

    Raises:
        ConfigError: one or more keys are missing or invalid. All problems are reported at once.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    problems: list[str] = []
    failed: set[str] = set()  # settings whose source already produced a problem
    values: dict[str, Any] = {}
    origin: dict[str, str] = {}  # setting name -> where its value came from, for messages

    config_path = _get(env, CONFIG_FILE_VAR)
    if config_path is not None:
        for name, value in _read_config_file(Path(config_path), problems).items():
            where = f"{name} (config file {config_path})"
            if _is_secret(name):
                problems.append(f"{where}: a secret is not read from the config file; {_how(name)}")
                failed.add(name)
                continue
            values[name] = value
            origin[name] = where

    for name in Settings.model_fields:
        key = _env_var(name)
        value = _get(env, key)
        if _is_secret(name) and (path := _get(env, key + _FILE_SUFFIX)) is not None:
            if value is not None:
                problems.append(f"{key}, {key}{_FILE_SUFFIX}: both are set; set only one")
                failed.add(name)
                continue
            key += _FILE_SUFFIX
            value = _read_secret_file(key, Path(path), problems)
            if value is None:
                failed.add(name)
                continue
        if value is not None:
            values[name] = value
            origin[name] = key

    settings: Settings | None = None
    try:
        settings = Settings.model_validate(values)
    except ValidationError as exc:
        # Built from locations and error types only: an error's input, message and context can
        # all carry the offending value.
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            name = str(error["loc"][0]) if error["loc"] else ""
            if name not in failed:
                problems.append(_describe(name, error["type"], origin))

    if settings is not None:
        problems.extend(_embedding_problems(settings, origin))
        problems.extend(_reranker_problems(settings, origin))
        problems.extend(_llm_problems(settings, origin))

    # Raised outside the except block, so the ValidationError is not chained as __context__.
    if settings is None or problems:
        raise ConfigError(problems)
    return settings


def _env_var(name: str) -> str:
    return ENV_PREFIX + name.upper()


def _is_secret(name: str) -> bool:
    field = Settings.model_fields.get(name)
    return field is not None and _mentions_secret(field.annotation)


def _mentions_secret(annotation: object) -> bool:
    return annotation is SecretStr or any(_mentions_secret(arg) for arg in get_args(annotation))


def _how(name: str) -> str:
    key = _env_var(name)
    return f"set {key} or {key}{_FILE_SUFFIX}"


def _get(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value or None


def _reason(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


# tomllib errors end in "(at line L, column C)" or "(at end of document)". Only that part is
# reported: the rest of the message can quote a character of the file.
_TOML_POSITION: Final = re.compile(r"\((at line \d+, column \d+|at end of document)\)")


def _read_config_file(path: Path, problems: list[str]) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except OSError as exc:
        problems.append(f"{CONFIG_FILE_VAR}: cannot read {path}: {_reason(exc)}")
    except UnicodeDecodeError:
        problems.append(f"{CONFIG_FILE_VAR}: {path} is not UTF-8 text")
    except tomllib.TOMLDecodeError as exc:
        match = _TOML_POSITION.search(str(exc))
        position = f" ({match.group(1)})" if match else ""
        problems.append(f"{CONFIG_FILE_VAR}: {path} is not valid TOML{position}")
    return {}


def _read_secret_file(key: str, path: Path, problems: list[str]) -> str | None:
    try:
        return path.read_bytes().decode("utf-8").rstrip("\r\n")
    except OSError as exc:
        problems.append(f"{key}: cannot read {path}: {_reason(exc)}")
    except UnicodeDecodeError:
        problems.append(f"{key}: {path} is not UTF-8 text")
    return None


def _describe(name: str, error_type: str, origin: Mapping[str, str]) -> str:
    where = origin.get(name, _env_var(name))
    if error_type == "missing":
        return f"{where}: required, not set; " + (
            _how(name) if _is_secret(name) else f"set {where} or {name} in the config file"
        )
    if error_type == "extra_forbidden":
        return f"{where}: unknown setting"
    field = Settings.model_fields.get(name)
    expected = f"; expected {field.description}" if field is not None and field.description else ""
    return f"{where}: invalid value ({error_type}){expected}"


# --- Embeddings: rules across settings -----------------------------------------------------------

_EMBEDDING_ONLY: Final[Mapping[str, EmbeddingProvider]] = {
    "embedding_url": "openai_compatible",
    "embedding_api_key": "openai_compatible",
    "embedding_model_dir": "local",
    "embedding_model_file": "local",
    "embedding_revision": "local",
    "embedding_pooling": "local",
    "embedding_max_tokens": "local",
}
"""Settings that apply to one provider only."""

_EMBEDDING_REQUIRED: Final[Mapping[EmbeddingProvider, tuple[str, ...]]] = {
    # The model and the URL have defaults; the dimension has one for the default model only.
    "openai_compatible": ("embedding_dimension",),
    "local": ("embedding_model", "embedding_dimension", "embedding_digest", "embedding_model_dir"),
}

_LOCAL_MODEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


def _embedding_problems(settings: Settings, origin: Mapping[str, str]) -> list[str]:
    """Problems of the embedding settings taken together. Each names its key, never a value."""
    provider = settings.embedding_provider
    given = [
        name
        for name in Settings.model_fields
        if name.startswith("embedding_") and name != "embedding_provider" and name in origin
    ]
    if provider is None:
        return [
            f"{origin[name]}: has no effect without embedding_provider;"
            f" set {_env_var('embedding_provider')}, or remove it"
            for name in given
        ]

    def where(name: str) -> str:
        return origin.get(name, _env_var(name))

    problems: list[str] = []
    for name in _EMBEDDING_REQUIRED[provider]:
        if getattr(settings, name) is None:
            key = _env_var(name)
            problems.append(
                f"{key}: required when embedding_provider is {provider};"
                f" set {key} or {name} in the config file"
            )
    for name in given:
        only = _EMBEDDING_ONLY.get(name)
        if only is not None and only != provider:
            problems.append(f"{origin[name]}: applies only when embedding_provider is {only}")
    if provider == "local":
        model, digest = settings.embedding_model, settings.embedding_digest
        if model is not None and _LOCAL_MODEL.fullmatch(model) is None:
            problems.append(
                f"{where('embedding_model')}: invalid value; expected a Hugging Face repository"
                " such as org/name"
            )
        if digest is not None and _SHA256_DIGEST.fullmatch(digest) is None:
            problems.append(
                f"{where('embedding_digest')}: invalid value; expected sha256: and 64 lowercase hex"
                " digits, the digest of the model files"
            )
    elif settings.embedding_url is not None and "@" in settings.embedding_url.split("/")[2]:
        key = _env_var("embedding_api_key")
        problems.append(
            f"{where('embedding_url')}: holds credentials, but the URL is not a secret;"
            f" set {key} or {key}{_FILE_SUFFIX} instead"
        )
    return problems


# --- Reranker: rules across settings -------------------------------------------------------------

_RERANKER_FREE: Final = frozenset(
    {"search_reranker", "search_rerank_depth", "search_reranker_model_dir"}
)
"""Reranker settings that may be set while ``search_reranker`` is ``none``: the depth is also what
``memini eval --reranker`` reorders, and the image sets the model directory whether the reranker
is on or not."""


def _reranker_problems(settings: Settings, origin: Mapping[str, str]) -> list[str]:
    """Problems of the reranker settings taken together. Each names its key, never a value."""
    given = [
        name
        for name in Settings.model_fields
        if name.startswith("search_reranker") and name not in _RERANKER_FREE and name in origin
    ]
    if settings.search_reranker == "none":
        return [
            f"{origin[name]}: has no effect while search_reranker is none;"
            f" set {_env_var('search_reranker')}=local, or remove it"
            for name in given
        ]
    problems: list[str] = []
    changed = [
        name
        for name, default in (
            ("search_reranker_model", DEFAULT_RERANKER_MODEL),
            ("search_reranker_model_file", DEFAULT_RERANKER_MODEL_FILE),
        )
        if getattr(settings, name) != default
    ]
    needed = ["search_reranker_digest"]
    if "search_reranker_model" in changed:
        needed.append("search_reranker_revision")
    for name in changed:
        for missing in needed:
            if missing not in origin:
                key = _env_var(missing)
                problems.append(
                    f"{origin[name]}: names other model files than the default, so it needs"
                    f" {missing} of its own; set {key} or {missing} in the config file"
                )
    return problems


# --- LLM providers: rules across settings --------------------------------------------------------

_LLM_SLOT_SETTINGS: Final = ("model", "api_key", "structured_output")
"""Settings of a provider slot that need its URL."""


def _llm_problems(settings: Settings, origin: Mapping[str, str]) -> list[str]:
    """Problems of the LLM settings taken together. Each names its key, never a value."""

    def where(name: str) -> str:
        return origin.get(name, _env_var(name))

    slots = (
        ("local", settings.llm_local_url, settings.llm_local_model),
        ("cloud", settings.llm_cloud_url, settings.llm_cloud_model),
    )
    problems: list[str] = []
    for slot, url, model in slots:
        url_name = f"llm_{slot}_url"
        if url is None:
            problems.extend(
                f"{origin[name]}: has no effect without {url_name}; set {_env_var(url_name)},"
                " or remove it"
                for name in (f"llm_{slot}_{suffix}" for suffix in _LLM_SLOT_SETTINGS)
                if name in origin
            )
            continue
        if model is None:
            key = _env_var(f"llm_{slot}_model")
            problems.append(
                f"{key}: required when {url_name} is set; set {key} or llm_{slot}_model in the"
                " config file"
            )
        if "@" in url.split("/")[2]:
            key = _env_var(f"llm_{slot}_api_key")
            problems.append(
                f"{where(url_name)}: holds credentials, but the URL is not a secret;"
                f" set {key} or {key}{_FILE_SUFFIX} instead"
            )
    if (
        settings.llm_local_url is None
        and settings.llm_cloud_url is None
        and "llm_timeout" in origin
    ):
        problems.append(
            f"{origin['llm_timeout']}: has no effect without llm_local_url or llm_cloud_url;"
            " set one, or remove it"
        )
    local_url, cloud_url = settings.llm_local_url, settings.llm_cloud_url
    if (
        local_url is not None
        and cloud_url is not None
        and local_url.rstrip("/") == cloud_url.rstrip("/")
        and settings.llm_local_model == settings.llm_cloud_model
    ):
        problems.append(
            f"{where('llm_cloud_url')}: names the same endpoint and model as llm_local_url; an"
            " endpoint is declared either local or cloud"
        )
    return problems
