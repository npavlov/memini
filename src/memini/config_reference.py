"""The configuration reference, ``docs/configuration.md``, generated from the settings model.

Regenerate the committed file after a change of :mod:`memini.config`, of the client variables
(:mod:`memini.cli.client`) or of what compose and the image set::

    make config-docs    # uv run python -m memini.config_reference docs/configuration.md

Without an argument the module prints the reference to stdout. A test fails while the committed file
and the code differ.

Where each part comes from:

- The settings, in field order: their environment variable, whether they are secrets, type,
  bounds, default and description, from :class:`memini.config.Settings`; the defaults of the
  default embedding model, from the constants the model validator applies; the settings that
  apply to one embedding provider, or that a provider requires, from the rules
  :func:`memini.config.load_settings` checks. Grouped by name prefix (:data:`GROUPS`); a setting
  that matches no group goes to the last one.
- What a change needs: :data:`REINDEX_SETTINGS`, the settings in the index fingerprint (a test
  checks it against what :func:`memini.embeddings.embedding_spec` reads); :data:`CHANGE_NOTES`
  for settings with a procedure of their own; a restart for every other setting.
- :data:`NOTES`: what a setting is for, or a rule across settings, where the description in the
  model gives only the form of the value. A description is also the "expected ..." text of an
  error message, which is why the model does not carry these.
- The client variables (:mod:`memini.cli.client`), the variables of compose.yaml, the settings
  compose and the image set, and the settings compose mounts as files: constants here, which tests
  check against that module, compose.yaml, the Dockerfile, .env.example and .env.memini.example.
"""

import re
import sys
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, TypeAliasType, Union, cast, get_args, get_origin

from pydantic import BeforeValidator, SecretStr
from pydantic.fields import FieldInfo

from memini.cli.client import (
    CLIENT_VARIABLES,
    CONNECT_TIMEOUT,
    DEFAULT_URL,
    TIMEOUT,
    TOKEN_FILE_VAR,
    TOKEN_VAR,
    URL_VAR,
)
from memini.config import (
    _EMBEDDING_ONLY,  # pyright: ignore[reportPrivateUsage] - documents the rule config checks
    _EMBEDDING_REQUIRED,  # pyright: ignore[reportPrivateUsage] - documents the rule config checks
    CONFIG_FILE_VAR,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_URL,
    DEFAULT_MODEL_SETTINGS,
    ENV_PREFIX,
    Settings,
    _byte_size,  # pyright: ignore[reportPrivateUsage] - recognizes the size settings
    _is_secret,  # pyright: ignore[reportPrivateUsage] - the rule load_settings applies
)

__all__ = [
    "CHANGE_NOTES",
    "COMPOSE_EMBEDDING_URL",
    "COMPOSE_ENV_FILE",
    "COMPOSE_FILE_SETTINGS",
    "COMPOSE_SECRETS_DIR",
    "COMPOSE_SET_SETTINGS",
    "COMPOSE_UNSET_SECRET",
    "COMPOSE_VARIABLES",
    "GROUPS",
    "IMAGE_SETTINGS",
    "NOTES",
    "REFERENCE_PATH",
    "REINDEX_SETTINGS",
    "TOML_EXAMPLE",
    "Group",
    "compose_file_variable",
    "compose_secret",
    "computed_defaults",
    "env_var",
    "is_secret",
    "main",
    "reference_text",
]

REFERENCE_PATH: Final = "docs/configuration.md"
"""Where the reference is committed, relative to the repository root."""

_FILE_SUFFIX: Final = "_FILE"


@dataclass(frozen=True, slots=True)
class Group:
    """A block of settings in the reference: a heading, the name prefixes it takes, an intro."""

    title: str
    prefixes: tuple[str, ...]
    intro: str


GROUPS: Final[tuple[Group, ...]] = (
    Group(
        "Database",
        ("database_",),
        "The Postgres database, with pgvector, that the service and the operator commands use."
        " Backup is a dump of it; the mirrors of git sources are fetched again.",
    ),
    Group(
        "HTTP server and request limits",
        ("http_",),
        "Where the server listens, and the limits of every HTTP and MCP request. A body over its"
        " limit is refused by its `Content-Length` before it is read, or as soon as more of it"
        " arrives (413); the ingest route has its own limit, `ingest_max_body_size`. The statement"
        " limits apply to the database statements a request runs; search has its own,"
        " `search_timeout`. Rate limits: per token, a budget of reads and one of writes; per"
        " client address, one for requests whose token is not known to be valid. Each budget is a"
        " minute's worth of requests, refilled steadily, and is kept in the process: budgets start"
        " full after a restart.",
    ),
    Group("Logging", ("log_",), "The service logs one JSON object per line, on stderr."),
    Group(
        "Metrics",
        ("metrics_",),
        "`GET /metrics` serves counts and timings in the Prometheus text format, to a scraper that"
        " sends `metrics_token` as its bearer token; without the token the route does not exist."
        " A Memini token is not accepted there. No metric is labelled by a workspace, source,"
        " stream or person; `memini_base_current` says what `GET /v1/status?gate=true` says.",
    ),
    Group(
        "Operator",
        ("principal",),
        "v1 has one user. The operator commands act as one principal, named here.",
    ),
    Group(
        "Embeddings",
        ("embedding_",),
        "Off until `embedding_provider` is set: the service starts without them, but does not"
        " sync git sources, and search refuses with a message that names the setting."
        " `openai_compatible` sends the text of chunks and queries to the API at `embedding_url`"
        " (Ollama, llama.cpp, vLLM or a cloud service): which endpoint, on this machine or not, is"
        " your explicit choice. `local` runs an ONNX model in the process; it needs the optional"
        " dependencies `memini[local-embeddings]` and pins the model files by `embedding_digest`,"
        " which is verified before they are loaded. A setting of the provider not chosen, or any"
        " `embedding_*` setting without a provider, is an error.",
    ),
    Group(
        "Search",
        ("search_",),
        "The limits of one search, and how a hybrid search fuses its retrievers: each contributes"
        " `search_candidates` results, merged by reciprocal rank fusion with k `search_rrf_k`,"
        " each retriever's ranks weighted.",
    ),
    Group(
        "Git connector",
        ("git_",),
        "The service keeps a bare mirror of every git source and reads code from it. Remote"
        " sources use https or ssh; plain http and local paths are refused unless allowed here."
        " Credentials are global for now: one https token and one ssh key.",
    ),
    Group(
        "Ingest",
        ("ingest_",),
        "The limits of one batch of records sent to the ingest API: the request body, the records"
        " in it, and the payload of one record as stored.",
    ),
    Group(
        "Credential store",
        ("credential_",),
        "The API keys and OAuth tokens of sources are stored encrypted (AES-256-GCM) with"
        " `credential_key`, which never enters the database. To rotate it: make a new key"
        " (`openssl rand -base64 32`), set it as `credential_key` and move the old one to"
        " `credential_old_keys`, and restart; run `memini credentials rotate-key`; when it"
        " reports nothing left, remove `credential_old_keys` and restart.",
    ),
    Group(
        "LLM providers",
        ("llm_",),
        "At most two OpenAI-compatible chat APIs, declared by the slot they are set in:"
        " `llm_local_*` is trusted with the content of every source, `llm_cloud_*` (https only)"
        " only with sources whose `llm_policy` is `cloud`, in workspaces that opted in"
        " (`memini workspace llm`). The slot is the declaration; it is never inferred from the"
        " URL. Each slot needs its URL and its model. No provider is configured by default, and"
        " then nothing is extracted.",
    ),
    Group("Other settings", (), "Settings that no group above takes yet."),
)
"""The blocks of the reference, in order. A setting goes to the first group with a prefix of its
name, or to the last group, which has none."""

REINDEX_SETTINGS: Final = frozenset(
    {
        "embedding_provider",
        "embedding_model",
        "embedding_digest",
        "embedding_dimension",
        "embedding_pooling",
        "embedding_max_tokens",
        "embedding_document_prefix",
    }
)
"""The settings in the index fingerprint (:mod:`memini.index`): a change of one of them blocks
search and indexing in every workspace indexed before it, until ``memini reindex`` rebuilds it. A
test checks that these are exactly the settings :func:`memini.embeddings.embedding_spec` reads."""

CHANGE_NOTES: Final[Mapping[str, str]] = {
    "principal": "nothing to restart (operator commands only); keep it after the first"
    " `memini bootstrap`",
    "embedding_model_file": "a new `embedding_digest`, which covers the file's path; so restart,"
    " then `memini reindex`",
    "git_mirror_dir": "restart; mirrors are not moved: the next sync of each source fetches its"
    " mirror anew",
    "credential_key": "restart with the old key in `credential_old_keys`, then"
    " `memini credentials rotate-key`",
    "credential_old_keys": "restart; remove them once `memini credentials rotate-key` reports"
    " nothing left",
}
"""What a change of a setting needs, where it is more than a restart or a reindex."""

NOTES: Final[Mapping[str, str]] = {
    "database_url": "Compose builds it from `MEMINI_DB_PASSWORD`.",
    "http_port": "The HTTP API and MCP (`/mcp`) listen on it.",
    "log_level": "The lowest level the service logs; any case.",
    "log_slow_statement_threshold": "A statement of the service over it is logged at WARNING with"
    " its duration, the request or run it served, and its text with every literal replaced by"
    " `?`, never its parameters.",
    "metrics_token": "Unset: there is no `/metrics` route. Prometheus sends it with"
    " `authorization: {credentials_file: ...}`; it reads counts only.",
    "principal": "The principal the operator commands act as, created on first use:"
    " `memini bootstrap` gives it the first admin token, so it owns what is made with that token.",
    "embedding_provider": "Unset: embeddings are off.",
    "embedding_digest": "For `local` it is verified before the model files are loaded. For"
    " `openai_compatible` it is recorded, not checked: after `ollama pull` brings a new build,"
    " set its digest.",
    "embedding_query_prefix": "Put before every search query before it is embedded.",
    "embedding_document_prefix": "Put before every chunk before it is embedded.",
    "embedding_url": "It receives the text of every chunk and query.",
    "embedding_api_key": "Sent to `embedding_url` in the `Authorization` header.",
    "embedding_model_dir": "The files of each digest get a directory of their own in it.",
    "embedding_model_file": "The digest covers this file and `tokenizer.json`, with their paths.",
    "embedding_revision": "Missing model files are fetched at this commit; without it, they must"
    " be in place.",
    "embedding_pooling": "How the token vectors of a text become one: their mean, or the vector"
    " of the first token.",
    "embedding_max_tokens": "A longer text is cut to this many tokens.",
    "git_local_roots": "With compose, mount the directory into the container as well; compose.yaml"
    " has an example.",
    "credential_key": "Unset: the service starts, and storing or reading a credential fails with"
    " a message that names this setting.",
    "llm_local_url": "Unset, and `llm_cloud_url` unset too: no LLM is called.",
    "llm_local_model": "Required when `llm_local_url` is set.",
    "llm_local_api_key": "Only with `llm_local_url`.",
    "llm_local_structured_output": "Only with `llm_local_url`. The reply is validated against"
    " the schema in every case.",
    "llm_cloud_url": "Not the same endpoint and model as `llm_local_url`.",
    "llm_cloud_model": "Required when `llm_cloud_url` is set.",
    "llm_cloud_api_key": "Only with `llm_cloud_url`.",
    "llm_cloud_structured_output": "Only with `llm_cloud_url`. The reply is validated against"
    " the schema in every case.",
    "llm_timeout": "Only with `llm_local_url` or `llm_cloud_url`.",
}
"""Sentences after a setting's description: what it is for, or a rule across settings."""

IMAGE_SETTINGS: Final[Mapping[str, str]] = {
    "http_host": "0.0.0.0",  # noqa: S104 - documents the image, which binds inside its container
    "http_port": "6364",
    "git_mirror_dir": "/var/lib/memini/mirrors",
}
"""The settings the Dockerfile sets in the image, by setting name. A test reads the Dockerfile."""

COMPOSE_VARIABLES: Final[Mapping[str, tuple[str, str]]] = {
    "MEMINI_HTTP_PORT": (
        "6364",
        "The port on the host's 127.0.0.1 the service is published on; MCP at"
        " `http://127.0.0.1:6364/mcp` by default. Not the setting `http_port`: in the container"
        " compose sets `MEMINI_HTTP_PORT` to 6364, where the service listens.",
    ),
    "MEMINI_DB_PORT": ("55432", "The port on the host's 127.0.0.1 the database is published on."),
    "MEMINI_DB_PASSWORD": (
        "memini",
        "The password of the database role, a development default; letters and digits only, as"
        " compose puts it into `MEMINI_DATABASE_URL` as it is. Set it before the first start: the"
        " database keeps the password it was created with.",
    ),
    "MEMINI_ENV_FILE": (
        ".env.memini",
        "The settings file, which compose passes into the service's container; relative to"
        " `compose.yaml`, or absolute. Optional: a file that does not exist is skipped.",
    ),
}
"""The variables compose.yaml reads on the host, besides the paths of the secret files: name,
default, description. A test reads compose.yaml."""

COMPOSE_ENV_FILE: Final = ".env.memini"
"""The settings file compose passes into the service's container unless ``MEMINI_ENV_FILE`` names
another; ``.env.memini.example`` lists the settings. Optional (``required: false``, which needs
Docker Compose 2.24.0)."""

COMPOSE_SET_SETTINGS: Final[Mapping[str, str]] = {
    "database_url": "the database of the stack, with `MEMINI_DB_PASSWORD`",
    "http_host": IMAGE_SETTINGS["http_host"],
    "http_port": IMAGE_SETTINGS["http_port"],
    "git_mirror_dir": IMAGE_SETTINGS["git_mirror_dir"],
}
"""The settings compose.yaml sets in the service's container, over a line of the settings file,
and what to: the database URL, and what the image sets too. ``MEMINI_HTTP_PORT`` is the port on the
host in ``.env``; a line meant for it in the settings file must not move the port inside. A test
reads compose.yaml."""

COMPOSE_FILE_SETTINGS: Final = (
    "embedding_api_key",
    "git_https_token",
    "git_ssh_key_path",
    "git_ssh_known_hosts_path",
    "credential_key",
    "credential_old_keys",
    "llm_local_api_key",
    "llm_cloud_api_key",
    "metrics_token",
)
"""The settings whose value compose.yaml mounts as a file, a compose secret: every secret but the
database URL, which compose sets itself, and the files of ssh. Each file's path on the host comes
from :func:`compose_file_variable` in ``.env``; the service reads it at
``COMPOSE_SECRETS_DIR/<compose_secret(name)>``. A test reads compose.yaml, and checks that every
secret setting is here."""

COMPOSE_SECRETS_DIR: Final = "/run/secrets"
"""Where compose mounts secrets in a container."""

COMPOSE_UNSET_SECRET: Final = "./secrets/unset"  # noqa: S105 - a path, of an empty file
"""The empty file compose mounts for a secret whose path is not set; the service does not read it,
as the variable that would point at it is empty."""

COMPOSE_EMBEDDING_URL: Final = "http://host.docker.internal:11434/v1"
"""The ``embedding_url`` of Ollama on the host, from inside a container: ``.env.memini.example``
sets it with the provider."""

TOML_EXAMPLE: Final = """\
log_level = "DEBUG"
http_port = 6364
git_local_roots = ["/srv/repos", "/home/me/src"]
git_max_blob_size = "2M"
"""
"""A config file shown in the reference. A test loads it."""

_CLIENT_NOTES: Final[Mapping[str, tuple[str, str]]] = {
    URL_VAR: (
        f"`{DEFAULT_URL}`",
        "The service's base URL, `http://` or `https://`, and TLS is whatever the URL says. A URL"
        " with a user name or password is refused: the token is the credential. `--url`"
        " overrides it.",
    ),
    TOKEN_VAR: (
        f"**required**, or `{TOKEN_FILE_VAR}`",
        "The token, a secret: sent as `Authorization: Bearer …`, never printed. No option takes"
        " the token itself, which shell history and the process list would keep. An admin"
        " creates one with `memini token create`; the first comes from `memini bootstrap`.",
    ),
    TOKEN_FILE_VAR: (
        "unset",
        f"The path of a file that holds the token; trailing line breaks are dropped. Setting"
        f" both it and `{TOKEN_VAR}` is an error. `--token-file` overrides both.",
    ),
}

_REINDEX_NEEDS: Final = "restart, then `memini reindex`"
_RESTART_NEEDS: Final = "restart"
_LONG_VALUE: Final = 40
"""A computed default longer than this is listed in the group's intro only, not in the table."""
_SIZE_UNITS: Final = (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024))


# --- Settings: names, kinds, values --------------------------------------------------------------


def env_var(name: str) -> str:
    """The environment variable of setting ``name``."""
    return ENV_PREFIX + name.upper()


def is_secret(name: str) -> bool:
    """Whether setting ``name`` is a secret: read from the environment only, also as ``_FILE``."""
    return _is_secret(name)


def compose_file_variable(name: str) -> str:
    """The variable that holds the file of setting ``name``, one of :data:`COMPOSE_FILE_SETTINGS`:
    ``MEMINI_NAME_FILE`` for a secret, the setting's own variable for a path. In ``.env`` it is the
    path on the host; compose sets it in the container to the path of the mounted file."""
    return env_var(name) + _FILE_SUFFIX if is_secret(name) else env_var(name)


def compose_secret(name: str) -> str:
    """The name of the compose secret that mounts the file of setting ``name``."""
    return name.removesuffix("_path")


def computed_defaults() -> dict[str, tuple[bool, object]]:
    """The defaults the model validator fills in when ``embedding_provider`` is
    ``openai_compatible``: setting name to (only while the model is the default one, value)."""
    computed: dict[str, tuple[bool, object]] = {
        "embedding_url": (False, DEFAULT_EMBEDDING_URL),
        "embedding_model": (False, DEFAULT_EMBEDDING_MODEL),
    }
    for name, value in DEFAULT_MODEL_SETTINGS.items():
        computed[name] = (True, value)
    return computed


def _group_of(name: str) -> Group:
    for group in GROUPS:
        if any(name.startswith(prefix) for prefix in group.prefixes):
            return group
    return GROUPS[-1]


def _unwrap(annotation: object) -> tuple[object, list[object]]:
    """The type without type aliases, ``Annotated`` and ``None``, and the metadata it carried."""
    metadata: list[object] = []
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            base, *extra = get_args(annotation)
            metadata.extend(extra)
            annotation = base
        elif get_origin(annotation) in (Union, types.UnionType):
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(args) != 1:
                return annotation, metadata
            annotation = args[0]
        else:
            return annotation, metadata


def _is_size(metadata: Sequence[object]) -> bool:
    return any(isinstance(item, BeforeValidator) and item.func is _byte_size for item in metadata)


_KINDS: Final[tuple[tuple[object, str], ...]] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (SecretStr, "string"),
    (Path, "path"),
)


def _kind(annotation: object, metadata: Sequence[object]) -> str:
    base, extra = _unwrap(annotation)
    if _is_size([*metadata, *extra]):
        return "size"
    if get_origin(base) is Literal:
        return "one of " + ", ".join(_code(str(value)) for value in get_args(base))
    if get_origin(base) is tuple:
        item = _kind(get_args(base)[0], ())
        return f"list, each {item}" if item.startswith("one of") else f"list of {item}s"
    for kind, text in _KINDS:
        if base is kind:
            return text
    return str(getattr(base, "__name__", base))


def _number(value: object, size: bool) -> str:
    if size and isinstance(value, int):
        return _size(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _size(value: int) -> str:
    for unit, factor in _SIZE_UNITS:
        if value >= factor and value % factor == 0:
            return f"{value // factor}{unit}"
    return str(value)


def _constraint(metadata: Sequence[object], attribute: str) -> object:
    for item in metadata:
        value: object = getattr(item, attribute, None)
        if value is not None:
            return value
    return None


def _bounds(metadata: Sequence[object], size: bool) -> str | None:
    def num(value: object) -> str:
        return _number(value, size)

    ge, gt = _constraint(metadata, "ge"), _constraint(metadata, "gt")
    le, lt = _constraint(metadata, "le"), _constraint(metadata, "lt")
    if ge is not None and le is not None:
        return f"{num(ge)} to {num(le)}"
    low = f"at least {num(ge)}" if ge is not None else None
    low = f"more than {num(gt)}" if gt is not None else low
    high = f"at most {num(le)}" if le is not None else None
    high = f"less than {num(lt)}" if lt is not None else high
    if low is not None or high is not None:
        return ", ".join(part for part in (low, high) if part is not None)

    shortest, longest = _constraint(metadata, "min_length"), _constraint(metadata, "max_length")
    if shortest is not None and longest is not None:
        return f"{shortest} to {longest} characters"
    if longest is not None:
        return f"at most {longest} characters"
    if shortest == 1:
        return "not empty"
    if shortest is not None:
        return f"at least {shortest} characters"
    return None


def _values(field: FieldInfo) -> str:
    _, extra = _unwrap(field.annotation)
    metadata = [*field.metadata, *extra]
    kind = _kind(field.annotation, field.metadata)
    bounds = _bounds(metadata, _is_size(metadata))
    return kind if bounds is None else f"{kind}, {bounds}"


def _code(text: str) -> str:
    return "`" + text.replace("\\", "\\\\").replace("\n", "\\n") + "`"


def _value(value: object, size: bool) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return _code("true" if value else "false")
    if value == "" or value == ():
        return "empty"
    if isinstance(value, tuple):
        return ", ".join(_value(item, size) for item in cast(tuple[object, ...], value))
    if isinstance(value, int | float):
        return _code(_number(value, size))
    return _code(str(value))


def _sentence(text: str, choices: Sequence[str]) -> str:
    """A description from the model as a sentence: capitalized, unless it starts with a name or a
    value, and with a full stop."""
    first = text.split(" ", 1)[0].rstrip(",:")
    if first.isalpha() and first.islower() and first not in choices:
        text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."


def _mark_settings(text: str) -> str:
    """``text`` with the setting names in it as code."""
    names = sorted(Settings.model_fields, key=len, reverse=True)
    pattern = re.compile(r"(?<![`\w])(" + "|".join(map(re.escape, names)) + r")(?![`\w])")
    return pattern.sub(r"`\1`", text)


# --- Settings: table cells -----------------------------------------------------------------------


def _setting_cell(name: str) -> str:
    lines = [_code(name), _code(env_var(name))]
    if is_secret(name):
        lines += [_code(env_var(name) + _FILE_SUFFIX), "*secret*"]
    return "<br>".join(lines)


def _default_cell(name: str, field: FieldInfo) -> str:
    _, extra = _unwrap(field.annotation)
    size = _is_size([*field.metadata, *extra])
    parts = ["**required**" if field.is_required() else _value(field.default, size)]
    computed = computed_defaults().get(name)
    if computed is not None:
        model_only, value = computed
        shown = _value(value, size)
        parts.append(
            f"with {_code('openai_compatible')}"
            + (" and its default model" if model_only else "")
            + ": "
            + (shown if len(shown) <= _LONG_VALUE else "see above")
        )
    providers = [p for p, required in _EMBEDDING_REQUIRED.items() if name in required]
    if len(providers) == len(_EMBEDDING_REQUIRED):
        parts.append("required with any provider")
    elif providers:
        parts.append("required with " + " and ".join(_code(p) for p in providers))
    if name in IMAGE_SETTINGS:
        parts.append(f"in the image: {_code(IMAGE_SETTINGS[name])}")
    return "; ".join(parts)


def _needs_cell(name: str) -> str:
    if name in CHANGE_NOTES:
        return CHANGE_NOTES[name]
    return _REINDEX_NEEDS if name in REINDEX_SETTINGS else _RESTART_NEEDS


def _description_cell(name: str, field: FieldInfo) -> str:
    parts: list[str] = []
    only = _EMBEDDING_ONLY.get(name)
    if only is not None:
        parts.append(f"*{_code(only)} only.*")
    if field.description:
        base, _ = _unwrap(field.annotation)
        choices = [str(v) for v in get_args(base)] if get_origin(base) is Literal else []
        parts.append(_mark_settings(_sentence(field.description, choices)))
    if name in NOTES:
        parts.append(NOTES[name])
    return " ".join(parts)


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [_row(header), _row(["---"] * len(header)), *(_row(row) for row in rows)]


# --- The document --------------------------------------------------------------------------------


def _intro() -> list[str]:
    example = env_var("http_port")
    return [
        "# Configuration reference",
        "",
        "<!-- Generated from src/memini/config.py by `make config-docs`"
        " (src/memini/config_reference.py). Do not edit by hand: a test fails while this file and"
        " the code differ. -->",
        "",
        "Every setting of the service and of its operator commands (`memini serve`, `migrate`,"
        " `bootstrap`, `reindex`, `credentials`, `workspace llm`, `eval`), with its default and"
        " what a change of it needs; then the variables of the client commands and of the compose"
        " stack.",
        "",
        "## Where settings come from",
        "",
        "Highest precedence first:",
        "",
        f"1. The environment: `{ENV_PREFIX}` and the setting name in upper case, so `http_port` is"
        f" `{example}`. A variable set to the empty string counts as not set; other `{ENV_PREFIX}*`"
        " variables are ignored.",
        f"2. A TOML file, if `{CONFIG_FILE_VAR}` holds its path (operator commands also take"
        f" `--config FILE`). Keys are setting names at the top level; an unknown key is an error,"
        " so a typo does not go unnoticed.",
        "3. The defaults below.",
        "",
        "```toml",
        *TOML_EXAMPLE.splitlines(),
        "```",
        "",
        f"**Secrets** come from the environment only, as `{ENV_PREFIX}NAME` (the value) or"
        f" `{ENV_PREFIX}NAME{_FILE_SUFFIX}` (the path of a file that holds it, as Docker and"
        " compose secrets provide it; trailing line breaks are dropped). Setting both is an error,"
        " and so is a secret in the TOML file, which holds nothing secret and can be shared. No log"
        " line, error message or output holds a secret's value.",
        "",
        "**Lists** are TOML arrays in the file, or comma-separated in the environment"
        f" (`{env_var('git_local_roots')}=/srv/repos,/home/me/src`). **Sizes** are a number of"
        " bytes, or a number with a binary unit: `512M`, `5G`, `1GiB` (1K = 1024 bytes).",
        "",
        "Settings are checked together at start. A missing or invalid one stops the service, or"
        " the operator command, with exit code 2 and one line per problem; each line names the"
        " setting and never its value.",
        "",
        "## What a change needs",
        "",
        f"- **{_RESTART_NEEDS}**: the service reads its settings once, at start. With compose,"
        " `docker compose up -d` recreates the container when its environment changed. Operator"
        " commands read the settings on every run.",
        f"- **{_REINDEX_NEEDS}**: the setting is part of the index fingerprint:"
        f" {', '.join(_code(name) for name in Settings.model_fields if name in REINDEX_SETTINGS)}."
        " After a change, search and indexing in a workspace whose index was built with other"
        " values are refused, with a message that names the command, until"
        " `memini reindex --workspace NAME` rebuilds that index.",
        "- A setting with a procedure of its own says so in the table.",
        "",
    ]


def _embedding_defaults() -> list[str]:
    computed = computed_defaults()
    lines = [
        "",
        "With `embedding_provider = openai_compatible` and nothing else set, these defaults apply"
        f" (`ollama pull {DEFAULT_EMBEDDING_MODEL}` first):",
        "",
    ]
    lines += [f"- {_code(n)}: {_value(v, False)}" for n, (only, v) in computed.items() if not only]
    lines.append(f"- and while the model is {_code(DEFAULT_EMBEDDING_MODEL)}:")
    lines += [f"  - {_code(n)}: {_value(v, False)}" for n, (only, v) in computed.items() if only]
    lines += [
        "",
        "A value that is set wins. Another model gets no dimension, digest or query prefix from"
        " here: it needs its own `embedding_dimension`.",
    ]
    return lines


def _settings_sections() -> list[str]:
    fields = Settings.model_fields
    lines = ["## Settings", ""]
    for group in GROUPS:
        names = [name for name in fields if _group_of(name) is group]
        if not names:
            continue
        lines += [f"### {group.title}", "", group.intro]
        if group.prefixes == ("embedding_",):
            lines += _embedding_defaults()
        rows = [
            [
                _setting_cell(name),
                _values(fields[name]),
                _default_cell(name, fields[name]),
                _needs_cell(name),
                _description_cell(name, fields[name]),
            ]
            for name in names
        ]
        lines += [
            "",
            *_table(["Setting", "Values", "Default", "Change needs", "Description"], rows),
            "",
        ]
    return lines


def _client_section() -> list[str]:
    rows = [[_code(name), *_CLIENT_NOTES[name]] for name in CLIENT_VARIABLES]
    return [
        "## Client commands",
        "",
        "Client commands (`memini workspace create`, `source`, `sync`, `runs`, `token`, `search`"
        " and the others that are not operator commands) are HTTP clients of the running service."
        " They read these variables from the environment, and nothing else: not the service's"
        " settings, nor its config file. A request may take"
        f" {_number(TIMEOUT, False)} seconds to answer, and {_number(CONNECT_TIMEOUT, False)} to"
        " connect; these are not settings.",
        "",
        *_table(["Variable", "Default", "Description"], rows),
        "",
    ]


def _compose_section() -> list[str]:
    variables = [
        [_code(name), _code(default), text] for name, (default, text) in COMPOSE_VARIABLES.items()
    ]
    pinned = ", ".join(
        f"{_code(env_var(name))} ({value if name == 'database_url' else _code(value)})"
        for name, value in COMPOSE_SET_SETTINGS.items()
    )
    files = [
        [
            _code(compose_file_variable(name)),
            _code(f"{COMPOSE_SECRETS_DIR}/{compose_secret(name)}"),
            _code(name),
        ]
        for name in Settings.model_fields
        if name in COMPOSE_FILE_SETTINGS
    ]
    unset = COMPOSE_UNSET_SECRET.removeprefix("./")
    return [
        "## Docker compose",
        "",
        "`compose.yaml` (Docker Compose 2.24.0 or later) takes its configuration from three"
        " places, none of them required. After a change, `docker compose up -d` recreates the"
        " service.",
        "",
        "### What compose reads: `.env`",
        "",
        "`compose.yaml` reads these variables from the environment, or from `.env` next to it"
        " (`.env.example` lists them), and the paths of the secret files below. They configure"
        " compose on the host; none of them is a setting of the service.",
        "",
        *_table(["Variable", "Default", "Description"], variables),
        "",
        "### The settings of the service: `.env.memini`",
        "",
        f"Compose passes every line of `{COMPOSE_ENV_FILE}`, or of the file `MEMINI_ENV_FILE`"
        " names, into the service's container as it is: one `MEMINI_NAME=value` per line, the"
        " variables of the settings above. So any setting can be set there, a new one included,"
        " without a change of `compose.yaml`; `.env.memini.example` lists them. Its values show"
        " in `docker inspect`: a secret goes into a file (below) instead.",
        "",
        f"Compose sets these itself, over a line of that file: {pinned}. Inside the container"
        " 127.0.0.1 is the container itself: with `embedding_provider`, set"
        f" `{env_var('embedding_url')}={COMPOSE_EMBEDDING_URL}` for Ollama on the host. The image"
        " has no in-process model, so `local` is not available there.",
        "",
        "### Secrets and other files",
        "",
        "Each of these variables, set in `.env`, names a file on the host: relative to"
        " `compose.yaml`, or absolute. Compose mounts it read-only into the container as a compose"
        " secret and sets the same variable there to the file's path in the container, where the"
        " service reads it; the file's content never shows in `docker inspect`. Unset, compose"
        f" mounts the empty `{unset}` instead and leaves the setting unset. The service runs as"
        " uid 10001 and must be able to read the file: on Linux it keeps its owner and mode in the"
        " container, so give it to that user (`sudo chown 10001 FILE`, mode 400); Docker Desktop"
        " and OrbStack let the container's user read it. ssh refuses a private key that others"
        " can read: keep the key's mode 600 or 400. Compose does not notice a change of a file's"
        " content: `docker compose up -d --force-recreate memini` reads it again.",
        "",
        *_table(["Variable", "In the container", "Setting"], files),
        "",
        "### Local repositories",
        "",
        "A local repository is a source only under `git_local_roots`, and the container sees no"
        " directory of the host until it is mounted: mount the directory that holds the"
        " repositories, read-only, and list its path in the container in"
        f" `{env_var('git_local_roots')}`. `compose.yaml` has an example under `volumes`.",
        "",
    ]


def reference_text() -> str:
    """The reference as Markdown, with a final newline."""
    lines = [*_intro(), *_settings_sections(), *_client_section(), *_compose_section()]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: Sequence[str]) -> int:
    """Print the reference, or write it to the one path given."""
    if not argv:
        sys.stdout.write(reference_text())
        return 0
    if len(argv) != 1:
        print("usage: python -m memini.config_reference [PATH]", file=sys.stderr)
        return 2
    Path(argv[0]).write_text(reference_text(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
