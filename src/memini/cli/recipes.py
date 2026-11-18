"""``memini recipe``: check recipe files, print the recipe JSON Schema.

- ``validate PATH...`` reads each recipe file and checks it (:mod:`memini.recipes.validate`). A
  valid file prints ``PATH: valid recipe NAME``; an invalid one prints one line per problem,
  ``PATH:LINE: FIELD: MESSAGE`` (``PATH: FIELD: MESSAGE`` when the field has no line in the
  file), naming the field by its keys, such as ``list.paging.next``. These lines are the
  command's result and go to stdout. A file that cannot be read as TOML (missing, not UTF-8, a
  TOML syntax error) is reported on stderr as ``memini: PATH: REASON``.
- ``schema`` prints the JSON Schema of a recipe file, as committed in
  ``schemas/recipe.schema.json``.

Both commands read files only: they need no database, no service and no setting.

Exit codes of ``validate``: 0 every file is a valid recipe; 1 a file is not; 2 a file cannot be
read (this wins over 1).
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from memini.cli.common import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, message
from memini.config import Settings
from memini.recipes import RecipeFileError, check_recipe, read_recipe
from memini.recipes.schema import schema_text

__all__ = [
    "DESCRIPTION",
    "HELP",
    "configure_parser",
    "schema_command",
    "validate_command",
    "validate_files",
]

HELP: Final = "check MCP recipe files; print the recipe JSON Schema"
DESCRIPTION: Final = (
    "A recipe describes how to sync one MCP server (docs/recipes.md). These commands read files"
    " only; they need no database and no service."
)


def configure_parser(group: argparse.ArgumentParser) -> None:
    """Add the commands of ``memini recipe`` to its parser, ``group``."""
    subcommands = group.add_subparsers(metavar="<command>", required=True)

    validate = subcommands.add_parser(
        "validate",
        help="check recipe files",
        description="Check each recipe file and print one line per problem, naming the field"
        " and its line, or one line for a valid file. Exit codes: 0 every file is valid, 1 a"
        " recipe is not valid, 2 a file cannot be read as TOML.",
    )
    validate.add_argument("paths", nargs="+", type=Path, metavar="PATH", help="a recipe file")
    validate.set_defaults(command=validate_command)

    schema = subcommands.add_parser(
        "schema",
        help="print the JSON Schema of a recipe file",
        description="Print the JSON Schema of a recipe file to stdout. It checks the structure"
        " of a recipe; `memini recipe validate` also checks the rules that span keys.",
    )
    schema.set_defaults(command=schema_command)


async def validate_command(args: argparse.Namespace, settings: Settings | None = None) -> int:
    """``memini recipe validate``. ``settings`` is not used: the command reads files only."""
    paths: list[Path] = args.paths
    return validate_files(paths)


async def schema_command(args: argparse.Namespace, settings: Settings | None = None) -> int:
    """``memini recipe schema``. Neither argument is used."""
    sys.stdout.write(schema_text())
    sys.stdout.flush()
    return EXIT_OK


def validate_files(paths: Sequence[Path], out: TextIO | None = None) -> int:
    """Check each file, print the result to ``out`` (stdout) and return the exit code."""
    stream = out if out is not None else sys.stdout
    unreadable = invalid = False
    for path in paths:
        try:
            text, data = read_recipe(path)
        except RecipeFileError as error:
            message(f"{path}: {error.reason}")
            unreadable = True
            continue
        recipe, issues = check_recipe(data, text)
        if recipe is not None:
            print(f"{path}: valid recipe {recipe.name}", file=stream)
            continue
        invalid = True
        for issue in issues:
            where = f"{path}:{issue.line}" if issue.line is not None else f"{path}"
            print(f"{where}: {issue}", file=stream)
    stream.flush()
    if unreadable:
        return EXIT_USAGE
    return EXIT_FAILURE if invalid else EXIT_OK
