"""``memini reindex --workspace NAME``: rebuild a workspace's index from raw records.

A thin wrapper of :func:`memini.index.reindex`; the work and its rules are there. The command
builds the embedder of the settings first, so that without an embedding provider it refuses before
it touches the database. Code records are read again from the repository mirrors under
``git_mirror_dir`` (:class:`memini.gitsync.MirrorContent`); a source without a mirror stops the
rebuild until its next sync has created one. It records the rebuild as a ``reindex`` run, prints
one line of counts when it is done, and is safe to run again after a failure: what was rebuilt is
kept.
"""

import argparse
from typing import Final

from memini.cli.common import EXIT_OK, CommandError, message, require_migrated
from memini.config import Settings
from memini.db import connect
from memini.embeddings import create_embedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import MirrorContent
from memini.index import (
    RawContentUnavailableError,
    ReindexInProgressError,
    ReindexResult,
    current_fingerprint,
    default_raw_content,
    reindex,
)
from memini.runs import record_run, wait_for_run_writes
from memini.workspaces import ensure_principal, list_owned_workspaces

__all__ = ["RUN_KIND", "reindex_command", "summary"]

RUN_KIND: Final = "reindex"


async def reindex_command(args: argparse.Namespace, settings: Settings) -> int:
    name: str = args.workspace
    embedder = create_embedder(settings)  # refuses without a provider, before the database
    code = MirrorContent(MirrorManager(GitOptions.from_settings(settings)))
    url = settings.database_url.get_secret_value()
    try:
        async with connect(url, autocommit=True) as conn, connect(url, autocommit=True) as runs:
            await require_migrated(conn, "run `memini migrate` first")
            owner_id = await ensure_principal(conn, settings.principal)
            found = [
                w for w in await list_owned_workspaces(conn, owner_id=owner_id) if w.name == name
            ]
            if not found:
                raise CommandError(f"no workspace named {name}; see `memini workspace list`")
            workspace = found[0]
            message(
                f"rebuilding the index of workspace {name} with"
                f" {current_fingerprint(embedder.spec).describe()}"
            )
            try:
                async with record_run(
                    runs, workspace_id=workspace.id, owner_id=owner_id, kind=RUN_KIND
                ):
                    result = await reindex(
                        conn,
                        embedder,
                        workspace_id=workspace.id,
                        raw=default_raw_content(conn, code=code),
                    )
            except ReindexInProgressError as error:
                raise CommandError(str(error)) from None
            except RawContentUnavailableError as error:
                raise CommandError(
                    f"{error}; the rebuild of workspace {name} stopped before it finished, and"
                    " search stays as it was"
                ) from None
            finally:
                await wait_for_run_writes()
    finally:
        await embedder.aclose()
    print(summary(result))
    return EXIT_OK


def summary(result: ReindexResult) -> str:
    """One line: what the rebuild did."""
    counts = result.counts
    return (
        f"rebuilt the index of workspace {result.workspace}: {result.records} records"
        f" ({result.records_indexed} indexed), {counts.chunks} chunks ({counts.inserted} new,"
        f" {counts.updated} changed, {counts.unchanged} unchanged, {counts.deleted} deleted),"
        f" {counts.embedded} texts embedded, {counts.reused} vectors reused"
    )
