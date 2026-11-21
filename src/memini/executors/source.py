"""What an mcp source is: ``source.config`` of a source of kind ``mcp``, and its checks.

An mcp source is an MCP server the service syncs with a recipe (requirements, section 4). Its
configuration, a JSON object::

    {"endpoint": {"url": "https://mcp.example.com/v1/mcp"},
     "recipe": {"toml": "format = 1\\nname = \\"hearth\\"\\n…", "name": "hearth",
                "digest": "<64 hex digits>"},
     "window": {"initial": "7d", "backfill": null},
     "filters": {"exclude_authors": ["*bot*"]}}

- ``endpoint``: where the server is. ``{"url": …}`` for streamable HTTP, checked against the egress
  policy (:mod:`memini.executors.egress`): an allowed endpoint, https, no credential in the URL. Or
  ``{"command": "/abs/path", "args": [...], "credential_env": "NAME"}`` for a stdio server the
  service starts, one of ``mcp_allowed_commands``; ``credential_env`` names the variable the
  source's credential goes into, if it has one. Checked when the source is added or changed, and
  again before every connection, by the settings as they are then. It can change: a server that
  moved is the same source.
- ``recipe``: the source holds its recipe. ``{"toml": …}`` is a recipe file's text, checked when
  it is set (a valid recipe, whose parser this build has) and stored with its ``name`` and
  ``digest`` (:func:`memini.recipes.recipe_digest`); ``memini source add --recipe PATH`` reads the
  file and sends its text, so the service needs no access to the client's files. ``{"bundled":
  NAME}`` is a recipe that ships with Memini (:mod:`memini.recipes.bundled`), read again at every
  sync, so a fix of it in a new build applies at once. A config written by other means may hold
  the TOML text itself, or the recipe as a table (the TOML read as JSON); both executors read
  every form through :func:`recipe_ref` and :func:`load_recipe_ref`.
- ``window``: the source's overrides of the recipe's ``[window]``
  (:func:`memini.intake.resolve_settings`): ``overlap``, ``initial``, ``backfill``, ``max_span``,
  ``history``, each a duration or null for none (``backfill``, ``max_span``, ``history``).
- ``filters``: as for a push source (:mod:`memini.filters`); ingest applies them.

Each stream of the source is one stream key of the recipe (a channel id, a mailbox query), kept as a
stream row, not here; a key must match the recipe's ``stream.key_pattern``. The credential is the
source's row in the credential store (``memini credentials set``), never part of the config.

:class:`McpConfig` reads a stored config without checking it again: a setting that changed since
(the allowlist, a command) is enforced before the next connection, and reading a source must not
fail on it. The checks raise :class:`McpConfigError`, which names the setting and the reason and
never the value; :mod:`memini.sources` turns it into its ``InvalidSourceError``.
"""

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Final, cast

from memini.executors.egress import EgressPolicy, EgressRefused

if TYPE_CHECKING:  # memini.recipes imports memini.ingest, which imports memini.sources' importers
    from memini.recipes import Recipe

__all__ = [
    "BUNDLED_PACKAGE",
    "MAX_ARGS",
    "MAX_ARG_LENGTH",
    "MAX_RECIPE_TEXT",
    "MAX_STREAMS",
    "MAX_STREAM_KEY_LENGTH",
    "Endpoint",
    "HttpEndpoint",
    "McpConfig",
    "McpConfigError",
    "RecipeRef",
    "StdioEndpoint",
    "bundled_recipe",
    "check_endpoint",
    "check_stream_keys",
    "check_window",
    "inline_recipe",
    "load_recipe_ref",
    "recipe_ref",
]

MAX_RECIPE_TEXT: Final = 256 * 1024
"""Bytes of a recipe a source holds, as UTF-8. A recipe is a page of TOML."""

MAX_ARGS: Final = 64
MAX_ARG_LENGTH: Final = 4096
MAX_STREAMS: Final = 100
"""Streams of one mcp source."""
MAX_STREAM_KEY_LENGTH: Final = 1024

BUNDLED_PACKAGE: Final = "memini.recipes.bundled"
"""Where the recipes that ship with Memini are: ``<name>.toml`` files."""

_ENV_NAME: Final = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_RECIPE_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_RESERVED_ENV: Final = frozenset({"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"})
"""The variables a stdio server inherits (the SDK's default list): a credential never replaces
one."""


class McpConfigError(ValueError):
    """A setting of an mcp source is refused: ``setting`` names it and ``reason`` says why,
    never quoting the value."""

    def __init__(self, setting: str, reason: str) -> None:
        super().__init__(f"{setting}: {reason}")
        self.setting = setting
        self.reason = reason


# --- Endpoint ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpEndpoint:
    """A server reached over streamable HTTP."""

    url: str

    def to_json(self) -> dict[str, Any]:
        return {"url": self.url}


@dataclass(frozen=True, slots=True)
class StdioEndpoint:
    """A server the service starts as a subprocess and talks to over its stdin and stdout."""

    command: str
    """The absolute path of the executable; one of ``mcp_allowed_commands``."""
    args: tuple[str, ...] = ()
    credential_env: str | None = None
    """The environment variable that carries the source's credential, if any."""

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"command": self.command, "args": list(self.args)}
        if self.credential_env is not None:
            value["credential_env"] = self.credential_env
        return value


type Endpoint = HttpEndpoint | StdioEndpoint


def check_endpoint(endpoint: Endpoint, policy: EgressPolicy) -> Endpoint:
    """``endpoint`` as it is stored, if the egress policy allows it: an allowed URL (normalized),
    or an allowed command with arguments and a variable name of the right form.

    Raises:
        McpConfigError: for ``endpoint.url``, ``endpoint.command``, ``endpoint.args`` or
            ``endpoint.credential_env``.
    """
    if isinstance(endpoint, HttpEndpoint):
        try:
            return HttpEndpoint(policy.check_url(endpoint.url).url)
        except EgressRefused as error:
            raise McpConfigError("endpoint.url", str(error)) from None
    try:
        command = policy.check_command(endpoint.command, endpoint.args, endpoint.credential_env)
    except EgressRefused as error:
        raise McpConfigError("endpoint.command", str(error)) from None
    if isinstance(endpoint.args, str):
        raise TypeError("args is a sequence of arguments, not one string")
    if len(endpoint.args) > MAX_ARGS or any(
        len(arg) > MAX_ARG_LENGTH or not arg.isprintable() for arg in endpoint.args
    ):
        raise McpConfigError(
            "endpoint.args",
            f"at most {MAX_ARGS} arguments of at most {MAX_ARG_LENGTH} printable characters",
        )
    credential_env = endpoint.credential_env
    if credential_env is not None and (
        _ENV_NAME.fullmatch(credential_env) is None or credential_env in _RESERVED_ENV
    ):
        raise McpConfigError(
            "endpoint.credential_env",
            "an environment variable name of upper-case letters, digits and _, not one the"
            f" server inherits ({', '.join(sorted(_RESERVED_ENV))})",
        )
    return StdioEndpoint(command=command, args=tuple(endpoint.args), credential_env=credential_env)


def _endpoint(value: object) -> Endpoint | None:
    if not isinstance(value, Mapping):
        return None
    data = cast(Mapping[str, Any], value)
    url, command = data.get("url"), data.get("command")
    if isinstance(url, str):
        return HttpEndpoint(url)
    if isinstance(command, str):
        args = data.get("args")
        credential_env = data.get("credential_env")
        return StdioEndpoint(
            command=command,
            args=tuple(a for a in cast(list[Any], args) if isinstance(a, str))
            if isinstance(args, list)
            else (),
            credential_env=credential_env if isinstance(credential_env, str) else None,
        )
    return None


# --- Recipe --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecipeRef:
    """The recipe of a source: its own text (or table), or a bundled one by name."""

    name: str
    """The recipe's ``name``; empty when the stored form does not say it."""
    toml: str | None = None
    """The text of the recipe the source holds; ``None`` for a bundled one or a table."""
    digest: str | None = None
    """:func:`memini.recipes.recipe_digest` of the text the source holds, when it was set; a
    bundled recipe's is recorded by each run that reads it."""
    table: Mapping[str, Any] | None = None
    """The recipe the source holds as a table (its TOML read as JSON), for a config written so."""

    @property
    def bundled(self) -> bool:
        return self.toml is None and self.table is None

    def to_json(self) -> Any:
        if self.table is not None:
            return dict(self.table)
        if self.toml is None:
            return {"bundled": self.name}
        return {"toml": self.toml, "name": self.name, "digest": self.digest}


def _parse(text: str, setting: str) -> "Recipe":
    if len(text.encode()) > MAX_RECIPE_TEXT:
        raise McpConfigError(setting, f"a recipe of at most {MAX_RECIPE_TEXT} bytes")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise McpConfigError(setting, f"not TOML: {error}") from None
    return _check(data, text, setting)


def _check(data: Mapping[str, Any], text: str | None, setting: str) -> "Recipe":
    from memini.recipes import RecipeError, parse_recipe

    try:
        return parse_recipe(data, text)
    except RecipeError as error:
        first = error.issues[0] if error.issues else None
        where = "" if first is None or first.line is None else f" (line {first.line})"
        raise McpConfigError(
            setting, f"not a valid recipe: {first}{where}; check it with `memini recipe validate`"
        ) from None


def _check_parser(recipe: "Recipe", setting: str) -> None:
    from memini.recipes import ParserNotFoundError, get_parser

    try:
        get_parser(recipe.parser.name)
    except ParserNotFoundError:
        raise McpConfigError(
            setting, f"this build has no parser named {recipe.parser.name}, which the recipe names"
        ) from None


def inline_recipe(text: str) -> tuple[RecipeRef, "Recipe"]:
    """The recipe a source holds, from its TOML text: valid, and with a parser in this build.

    Raises:
        McpConfigError: for ``recipe``.
    """
    from memini.recipes import recipe_digest

    recipe = _parse(text, "recipe")
    _check_parser(recipe, "recipe")
    return RecipeRef(name=recipe.name, toml=text, digest=recipe_digest(recipe)), recipe


def _bundled_text(name: str) -> str:
    if _RECIPE_NAME.fullmatch(name) is None:
        raise McpConfigError("recipe", "a bundled recipe's name is a recipe name")
    try:
        found = resources.files(BUNDLED_PACKAGE).joinpath(f"{name}.toml")
        return found.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError, IsADirectoryError):
        raise McpConfigError("recipe", f"no recipe named {name} ships with this build") from None


def bundled_recipe(name: str) -> tuple[RecipeRef, "Recipe"]:
    """The recipe named ``name`` among those that ship with Memini (:data:`BUNDLED_PACKAGE`).

    Raises:
        McpConfigError: for ``recipe``: no such recipe, or it or its parser is broken.
    """
    recipe = _parse(_bundled_text(name), "recipe")
    _check_parser(recipe, "recipe")
    return RecipeRef(name=name), recipe


def load_recipe_ref(ref: RecipeRef) -> "Recipe":
    """The recipe ``ref`` names, read and checked now: the text the source holds, or the bundled
    file of this build. The parser is not looked up: :func:`memini.recipes.get_parser`.

    Raises:
        McpConfigError: the recipe is not valid (any more), or no longer ships.
    """
    if ref.table is not None:
        return _check(ref.table, None, "recipe")
    if ref.toml is not None:
        return _parse(ref.toml, "recipe")
    return _parse(_bundled_text(ref.name), "recipe")


def _toml_name(text: str) -> str:
    try:
        name = tomllib.loads(text).get("name")
    except tomllib.TOMLDecodeError:
        return ""
    return name if isinstance(name, str) else ""


def recipe_ref(value: object) -> RecipeRef | None:
    """The recipe a source's config holds (``source.config.recipe``), as stored, not checked;
    ``None`` when it holds none. The forms: ``{"toml", "name", "digest"}`` and ``{"bundled"}``,
    which the administration writes; the TOML text itself; the recipe as a table."""
    if isinstance(value, str):
        return RecipeRef(name=_toml_name(value), toml=value)
    if not isinstance(value, Mapping):
        return None
    data = cast(Mapping[str, Any], value)
    bundled, toml, name, digest = (
        data.get("bundled"),
        data.get("toml"),
        data.get("name"),
        data.get("digest"),
    )
    if isinstance(bundled, str) and len(data) == 1:
        return RecipeRef(name=bundled)
    if isinstance(toml, str):
        return RecipeRef(
            name=name if isinstance(name, str) else "",
            toml=toml,
            digest=digest if isinstance(digest, str) else None,
        )
    return RecipeRef(name=name if isinstance(name, str) else "", table=dict(data))


# --- Streams and windows -------------------------------------------------------------------------


def check_stream_keys(keys: Sequence[str], recipe: "Recipe") -> tuple[str, ...]:
    """The stream keys as they are stored: sorted, without duplicates, each matching the recipe's
    ``stream.key_pattern`` in full, at least one and at most :data:`MAX_STREAMS`.

    Raises:
        McpConfigError: for ``streams``; the reason quotes the recipe's ``key_description``.
    """
    if isinstance(keys, str):
        raise TypeError("streams is a sequence of stream keys, not one string")
    unique = sorted(set(keys))
    if not unique:
        raise McpConfigError(
            "streams", f"an mcp source has at least one stream: {recipe.stream.key_description}"
        )
    if len(unique) > MAX_STREAMS:
        raise McpConfigError("streams", f"an mcp source has at most {MAX_STREAMS} streams")
    pattern = recipe.stream.key_pattern
    compiled = None if pattern is None else re.compile(pattern)
    for key in unique:
        if (
            not key
            or len(key) > MAX_STREAM_KEY_LENGTH
            or not key.isprintable()
            or (compiled is not None and compiled.fullmatch(key) is None)
        ):
            raise McpConfigError(
                "streams",
                f"a stream key is {recipe.stream.key_description}, printable, at most"
                f" {MAX_STREAM_KEY_LENGTH} characters"
                + ("" if compiled is None else ", matching the recipe's stream.key_pattern"),
            )
    return tuple(unique)


def check_window(window: Mapping[str, str | None], recipe: "Recipe") -> dict[str, str | None]:
    """The source's overrides of the recipe's ``[window]``, as stored: the keys of
    :data:`memini.intake.OVERRIDABLE`, each a duration such as ``7d``, or null where none is
    allowed. Checked with the recipe, as the planner will read them.

    Raises:
        McpConfigError: for ``window.<key>``.
    """
    from memini.intake import resolve_settings

    stored: dict[str, str | None] = {}
    for key, value in window.items():
        if value is not None and not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise McpConfigError(f"window.{key}", "a duration such as 7d, or null")
        stored[key] = value
    try:
        resolve_settings(recipe.window, stored)
    except ValueError as error:
        setting, _, reason = str(error).partition(": ")
        raise McpConfigError(setting or "window", reason or "not valid") from None
    return stored


def _window(value: object) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        return {}
    data = cast(Mapping[str, Any], value)
    return {key: item for key, item in data.items() if item is None or isinstance(item, str)}


# --- The configuration ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpConfig:
    """``source.config`` of an mcp source, filters aside (``Source.filters``). See the module."""

    endpoint: Endpoint | None
    """``None`` when the stored config holds none that can be read."""
    recipe: RecipeRef | None
    window: Mapping[str, str | None]

    def to_json(self) -> dict[str, Any]:
        """The JSON object stored in ``source.config``, without its ``filters``."""
        value: dict[str, Any] = {"window": dict(self.window)}
        if self.endpoint is not None:
            value["endpoint"] = self.endpoint.to_json()
        if self.recipe is not None:
            value["recipe"] = self.recipe.to_json()
        return value

    @classmethod
    def from_json(cls, config: Mapping[str, Any]) -> "McpConfig":
        """The configuration as stored, not checked again (see the module)."""
        return cls(
            endpoint=_endpoint(config.get("endpoint")),
            recipe=recipe_ref(config.get("recipe")),
            window=_window(config.get("window")),
        )
