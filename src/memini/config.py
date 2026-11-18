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

``principal`` (``MEMINI_PRINCIPAL``, default ``owner``) names the principal the CLI acts as: it owns
the workspaces and tokens the CLI creates, and is created on first use. v1 has one user, so the
default fits; set it only to give that principal another name, and keep it the same afterwards.

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
"""

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, ValidationError

__all__ = [
    "CONFIG_FILE_VAR",
    "EMBEDDING_MAX_DIMENSION",
    "ENV_PREFIX",
    "HANDLE_REGEX",
    "ConfigError",
    "EmbeddingPooling",
    "EmbeddingProvider",
    "LogLevel",
    "Settings",
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

EMBEDDING_MAX_DIMENSION: Final = 16000
"""The largest dimension a pgvector ``vector`` column holds."""

HANDLE_REGEX: Final = r"^[a-z0-9][a-z0-9_-]{0,62}$"
"""A valid name of a principal, as :data:`memini.workspaces.HANDLE_PATTERN` and the database check
it. Repeated here so that this module imports no database code; a test keeps the two equal."""


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


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
        description="the base URL of an OpenAI-compatible API, e.g. http://127.0.0.1:11434/v1",
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
    "openai_compatible": ("embedding_model", "embedding_dimension", "embedding_url"),
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
