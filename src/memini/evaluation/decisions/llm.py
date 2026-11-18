"""The ``llm`` variant of the decision eval: the pipeline's extractor and classifier
(:mod:`memini.extraction.llm`) through the LLM gate, on the local provider of the settings.

The gate decides by the ``llm_policy`` of a workspace and of the sources whose content is in a
request, which it reads from the database. So the variant creates a scratch database on the server
of ``database_url``, as the retrieval eval does (:func:`memini.evaluation.runner.scratch_database`;
the role needs ``CREATEDB``), with one workspace per set and one push source per source of its
records, all of policy ``local``, the default: every request goes to the local provider, and a
configured cloud provider is never used. Records are not stored there: the extractor reads them
from the set, and the replay of the journal is the runner's. The database is dropped by
:attr:`~memini.evaluation.decisions.runner.Pipeline.aclose`.

A reply that does not match the schema (:class:`~memini.llm.LlmOutputError`) counts as nothing
extracted, or as ``new``, and is counted in the summary, so one bad reply does not end the run; any
other error of the provider ends it.
"""

import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID

import anyio
import httpx2
from psycopg import AsyncConnection

from memini.auth import WriteAccess
from memini.config import Settings
from memini.db import connect, migrate
from memini.evaluation.decisions.dataset import DecisionSet
from memini.evaluation.decisions.runner import DecisionEvalError, Pipeline
from memini.evaluation.runner import scratch_database
from memini.extraction import Candidate, ProposedItem, RecordInput, Verdict
from memini.extraction.llm import LlmDecisions, extractor_name
from memini.llm import LlmGate, LlmOutputError, configured_providers, create_llm_gate
from memini.sources import NewPushSource, create_push_source
from memini.workspaces import create_workspace, ensure_principal

__all__ = ["PRINCIPAL", "LlmEvalPipeline", "create_llm_pipeline"]

log = logging.getLogger(__name__)

PRINCIPAL = "eval"
"""The principal that owns the scratch workspaces."""


class LlmEvalPipeline:
    """An :class:`~memini.extraction.Extractor` and
    :class:`~memini.extraction.SupersessionClassifier` on the gate; see the module."""

    def __init__(
        self,
        sets: Sequence[DecisionSet],
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        providers = configured_providers(settings)
        local = providers.get("local")
        if local is None:
            raise DecisionEvalError(
                "the llm extractor runs on the local LLM provider: set MEMINI_LLM_LOCAL_URL and"
                " MEMINI_LLM_LOCAL_MODEL"
            )
        self.name = extractor_name(local.model)
        self._gate: LlmGate = create_llm_gate(settings, transport=transport)
        self._decisions = LlmDecisions(self._gate)
        self._server_url = settings.database_url.get_secret_value()
        self._sets = tuple(sets)
        self._stack = AsyncExitStack()
        self._lock = anyio.Lock()
        self._conn: AsyncConnection[Any] | None = None
        self._workspaces: dict[str, UUID] = {}
        self._sources: dict[tuple[str, str], UUID] = {}
        self.database: str | None = None
        """The scratch database's name, once it exists."""
        self.rejected = 0
        self.output_errors = 0

    async def _ready(self) -> AsyncConnection[Any]:
        async with self._lock:
            if self._conn is not None:
                return self._conn
            name, url = await self._stack.enter_async_context(scratch_database(self._server_url))
            self.database = name
            await migrate(url)
            conn = await self._stack.enter_async_context(connect(url, autocommit=True))
            owner = await ensure_principal(conn, PRINCIPAL)
            access = WriteAccess.for_owner(owner)
            for data in self._sets:
                workspace = await create_workspace(conn, owner_id=owner, name=data.workspace)
                self._workspaces[data.workspace] = workspace.id
                for source in sorted({record.ref.source for record in data.records}):
                    created = await create_push_source(
                        conn, access, data.workspace, NewPushSource(name=source)
                    )
                    self._sources[(data.workspace, source)] = created.id
            self._conn = conn
            return conn

    def _scope(self, workspace: str, sources: set[str]) -> tuple[UUID, set[UUID]]:
        try:
            return self._workspaces[workspace], {self._sources[(workspace, s)] for s in sources}
        except KeyError as error:
            raise DecisionEvalError(f"no scratch workspace or source for {error}") from None

    async def extract(self, record: RecordInput) -> list[ProposedItem]:
        conn = await self._ready()
        workspace_id, source_ids = self._scope(record.workspace, {record.ref.source})
        try:
            extracted = await self._decisions.extract(
                conn, workspace_id=workspace_id, source_ids=source_ids, record=record
            )
        except LlmOutputError as error:
            self.output_errors += 1
            log.warning("extraction of %s: %s", record.ref, error)
            return []
        self.rejected += extracted.rejected
        return list(extracted.items)

    async def classify(
        self, item: ProposedItem, record: RecordInput, candidates: Sequence[Candidate]
    ) -> Verdict:
        conn = await self._ready()
        sources = {record.ref.source}
        sources |= {e.record.source for candidate in candidates for e in candidate.evidence}
        workspace_id, source_ids = self._scope(record.workspace, sources)
        try:
            return await self._decisions.classify(
                conn,
                workspace_id=workspace_id,
                source_ids=source_ids,
                item=item,
                record=record,
                candidates=candidates,
            )
        except LlmOutputError as error:
            self.output_errors += 1
            log.warning("supersession check of an item of %s: %s", record.ref, error)
            return Verdict()

    def summary(self) -> str:
        totals = self._decisions.totals
        provider = self._gate.providers["local"]
        return (
            f"{self.name} at {provider.target}: {totals.calls} calls,"
            f" {totals.input_tokens} input and {totals.output_tokens} output tokens;"
            f" {self.rejected} items rejected without a valid anchor;"
            f" {self.output_errors} replies that did not match the schema"
        )

    async def aclose(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            await self._gate.aclose()


def create_llm_pipeline(
    sets: Sequence[DecisionSet],
    settings: Settings | None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Pipeline:
    """The ``llm`` variant's pipeline; ``transport`` is for tests (a fake chat server).

    Raises:
        DecisionEvalError: no settings, or no local LLM provider in them.
    """
    if settings is None:
        raise DecisionEvalError("the llm extractor needs the service's settings")
    pipeline = LlmEvalPipeline(sets, settings, transport=transport)
    return Pipeline(pipeline, pipeline, aclose=pipeline.aclose, summary=pipeline.summary)
