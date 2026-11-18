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
    "ENV_PREFIX",
    "ConfigError",
    "LogLevel",
    "Settings",
    "environment_variables",
    "load_settings",
]

ENV_PREFIX: Final = "MEMINI_"
CONFIG_FILE_VAR: Final = "MEMINI_CONFIG_FILE"
_FILE_SUFFIX: Final = "_FILE"

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


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
