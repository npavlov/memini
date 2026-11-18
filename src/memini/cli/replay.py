"""``memini replay --workspace NAME --source NAME --recipe PATH [--stream KEY] [--run ID]``:
parse an mcp source's stored responses again and ingest what the parser builds, without calling
the source.

An operator command, as ``reindex`` is: it re-derives stored data once a build with a fixed parser
is deployed, on the database with the service's settings, and needs no token. A thin wrapper of
:func:`memini.responses.replay`; the rules are there. The recipe is read from ``--recipe`` (until a
source holds its recipe) and its parser is found by its ``parser.name`` among the parsers of this
build. The command holds the source's job lock while it runs, records a ``replay`` run, and prints
one line of counts. With an embedding provider, the records it writes are indexed with them;
without one they are stored without chunks, and ``memini reindex`` indexes them later.

Exit codes: 0 done; 1 no such workspace, source or stream, a job of the source is running, the
parser failed or a window came from another server (nothing written then), or ingest refused a
batch; 2 a recipe file that cannot be read or is not valid, or a parser this build lacks.
"""

import argparse
import uuid
from pathlib import Path
from typing import Final

from memini.auth import WriteAccess
from memini.cli.arguments import parse_source_name, parse_workspace_name
from memini.cli.common import EXIT_OK, EXIT_USAGE, CommandError, message, require_migrated
from memini.config import Settings
from memini.db import connect
from memini.embeddings import EmbeddingsNotConfiguredError, create_embedder
from memini.extraction.job import extraction_enabled
from memini.ingest import IngestLimits, PayloadIndexer
from memini.recipes import (
    ParserNotFoundError,
    RecipeError,
    RecipeFileError,
    get_parser,
    load_recipe,
)
from memini.responses import ReplayError, ReplayResult, replay
from memini.runs import wait_for_run_writes
from memini.sources import SourceBusyError
from memini.workspaces import NotFoundError, ensure_principal

__all__ = ["DESCRIPTION", "HELP", "configure_parser", "replay_command", "summary"]

HELP: Final = "parse an mcp source's stored responses again, without calling the source"
DESCRIPTION: Final = (
    "Parse the stored raw responses of an mcp source again with the recipe's parser in this"
    " build and ingest the records it builds, without calling the source: after a parser fix."
    " Replays every window of the source, or of one stream or one run, in the order they were"
    " fetched; a record goes only from the last window that holds it, so a replay with an"
    " unchanged parser writes nothing. Refused while a job of the source runs."
)


def configure_parser(parser: argparse.ArgumentParser) -> None:
    """Add the arguments of ``memini replay`` to its parser."""
    parser.add_argument(
        "--workspace",
        metavar="NAME",
        required=True,
        type=parse_workspace_name,
        help="the source's workspace",
    )
    parser.add_argument(
        "--source", metavar="NAME", required=True, type=parse_source_name, help="an mcp source"
    )
    parser.add_argument(
        "--recipe",
        metavar="PATH",
        required=True,
        type=Path,
        help="the source's recipe file; its parser.name picks the parser",
    )
    parser.add_argument("--stream", metavar="KEY", help="replay only this stream's windows")
    parser.add_argument(
        "--run", metavar="ID", type=uuid.UUID, help="replay only the windows of this run"
    )
    parser.set_defaults(command=replay_command)


async def replay_command(args: argparse.Namespace, settings: Settings) -> int:
    path: Path = args.recipe
    try:
        recipe = load_recipe(path)
    except RecipeFileError as error:
        raise CommandError(f"{path}: {error.reason}", EXIT_USAGE) from None
    except RecipeError as error:
        first = error.issues[0] if error.issues else None
        raise CommandError(
            f"{path}: not a valid recipe ({first}); see `memini recipe validate`", EXIT_USAGE
        ) from None
    try:
        parser = get_parser(recipe.parser.name)
    except ParserNotFoundError as error:
        raise CommandError(str(error), EXIT_USAGE) from None
    try:
        embedder = create_embedder(settings)
    except EmbeddingsNotConfiguredError:
        embedder = None
    url = settings.database_url.get_secret_value()
    try:
        async with connect(url, autocommit=True) as conn, connect(url, autocommit=True) as runs:
            await require_migrated(conn, "run `memini migrate` first")
            owner_id = await ensure_principal(conn, settings.principal)
            try:
                result = await replay(
                    conn,
                    WriteAccess.for_owner(owner_id),
                    args.workspace,
                    args.source,
                    recipe=recipe,
                    parser=parser,
                    stream=args.stream,
                    run_id=args.run,
                    limits=IngestLimits.from_settings(settings),
                    runs=runs,
                    indexer=None if embedder is None else PayloadIndexer(embedder),
                    extract=extraction_enabled(settings),
                )
            except NotFoundError as error:
                raise CommandError(f"{error}: see `memini source list {args.workspace}`") from None
            except (SourceBusyError, ReplayError) as error:
                raise CommandError(str(error)) from None
            finally:
                await wait_for_run_writes()
    finally:
        if embedder is not None:
            await embedder.aclose()
    print(summary(args.source, result))
    if embedder is None and result.inserted + result.updated + result.revived:
        message(
            "no embedding provider is configured: the records were stored without chunks;"
            " set MEMINI_EMBEDDING_PROVIDER, then run memini reindex"
        )
    return EXIT_OK


def summary(source: str, result: ReplayResult) -> str:
    """One line: what the replay did."""
    return (
        f"replayed {result.windows} windows of source {source} ({result.windows_complete}"
        f" complete, {result.responses} responses): {result.records} records,"
        f" {result.inserted} new, {result.updated} changed, {result.revived} revived,"
        f" {result.unchanged} unchanged; left out: {result.superseded} held by a later window,"
        f" {result.kept_deleted} deleted at the source, {result.expired} expired,"
        f" {result.forgotten} forgotten; 0 calls to the source"
    )
