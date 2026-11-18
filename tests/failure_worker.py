"""A job worker in a process of its own, for the crash test of tests/test_failures_db.py:
``python failure_worker.py CONFIG`` runs ``git_sync`` jobs from the database the JSON file CONFIG
names, as ``memini serve`` runs them, until it is killed.

Its embedding model (the FakeEmbedder) stops answering at the model call the config names
(``hang_at``, 1-based): it first writes the file ``marker``, then waits for ever, in the middle of
a batch's transaction. The test waits for the file and kills the process with SIGKILL there, at a
known point, with the batches before committed and one open. Everything here is synthetic.
"""

import json
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio

from memini.db import open_pool
from memini.embeddings import FakeEmbedder
from memini.git import GitOptions, MirrorManager
from memini.gitsync import GIT_SYNC, SyncLimits, git_sync_handler
from memini.jobs import RetryPolicy, Worker
from memini.logs import configure_logging


class HangingEmbedder(FakeEmbedder):
    """The FakeEmbedder, which never answers its call number ``hang_at``."""

    def __init__(self, *, hang_at: int, marker: Path) -> None:
        super().__init__()
        self.hang_at = hang_at
        self.marker = marker

    async def _embed_batch(self, texts: list[str]) -> Sequence[Sequence[float]]:
        if len(self.batches) + 1 == self.hang_at:
            self.marker.write_text("hanging\n")
            await anyio.sleep_forever()
        return await super()._embed_batch(texts)


async def main(config: dict[str, Any]) -> None:
    configure_logging("INFO")
    options = GitOptions(
        mirror_dir=Path(config["mirror_dir"]),
        local_roots=tuple(Path(root) for root in config["local_roots"]),
        max_blob_size=int(config["max_blob_size"]),
        fetch_timeout=30,
        command_timeout=30,
    )
    embedder = HangingEmbedder(hang_at=int(config["hang_at"]), marker=Path(config["marker"]))
    lease = timedelta(seconds=float(config["lease"]))
    async with open_pool(config["database_url"], min_size=1, max_size=4, timeout=10) as pool:
        handler = git_sync_handler(
            pool,
            MirrorManager(options),
            embedder,
            limits=SyncLimits(batch_files=int(config["batch_files"])),
        )
        worker = Worker(
            pool,
            {GIT_SYNC: handler},
            name=str(config["name"]),
            lease=lease,
            heartbeat=lease / 4,
            poll_interval=timedelta(milliseconds=50),
            retry=RetryPolicy(base=timedelta(seconds=30)),
        )
        await worker.run()


if __name__ == "__main__":
    anyio.run(main, json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
