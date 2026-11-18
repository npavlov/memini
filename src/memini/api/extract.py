"""The re-extraction route: ``POST /v1/workspaces/{workspace}/extract``, the HTTP face of
:func:`memini.extraction.reextract.enqueue_reextraction` (requirements, section 5, Re-extraction).

It enqueues an ``extract`` job for each live thread and meeting of the workspace whose current
content the current extractor of its source has not read: records stored before an LLM provider
was configured, and every record after the extractor changed (a new prompt version or another
model). The body narrows it to some ``sources`` and to records that occurred ``since`` a time;
``dry_run`` counts without enqueuing. A job of the same content that is queued or running already
is not doubled, so a second call enqueues nothing while the jobs wait. The answer counts the
records per source with its current extractor: 202 when jobs were enqueued (they run when the
service's worker takes them; their runs appear under ``GET /v1/workspaces/{workspace}/runs``), 200
for a dry run. Needs the ``admin`` scope: the jobs send the records to an LLM, which costs time,
and on a cloud provider, money.

Errors: 404 ``not_found`` for a workspace or source the token may not change or that does not
exist.
"""

from datetime import datetime
from typing import Annotated, Final

from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from memini.api.admin_errors import admin_errors
from memini.api.auth import AdminAccess
from memini.api.database import Connection
from memini.config import HANDLE_REGEX, Settings
from memini.extraction.reextract import (
    Reextraction,
    ReextractionCounts,
    SourceReextraction,
    enqueue_reextraction,
)
from memini.llm import configured_providers

__all__ = ["ExtractAccepted", "ExtractRequest", "build_extract_router"]

MAX_SOURCES: Final = 100
"""Sources one request may name."""


def _time_string(value: object) -> object:
    # A number would pass as seconds since the epoch; a time is written out, with its zone.
    if not isinstance(value, str):
        raise ValueError("a time is a string")  # pydantic reports a ValueError as value_error
    return value


class ExtractRequest(BaseModel):
    """Which records to look at; every live thread and meeting of the workspace by default."""

    model_config = ConfigDict(extra="forbid")

    sources: list[Annotated[str, Field(pattern=HANDLE_REGEX)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_SOURCES,
        description="The sources by name; null or left out: every source of the workspace.",
    )
    since: Annotated[AwareDatetime, BeforeValidator(_time_string)] | None = Field(
        default=None,
        description="Only records that occurred at or after this time (ISO 8601 with a zone).",
    )
    dry_run: bool = Field(default=False, description="Count the records and enqueue nothing.")


class ExtractCounts(BaseModel):
    """Records by what became of them: records = current + enqueued + queued + skipped."""

    records: int = Field(
        description="Live threads and meetings of the sources (since the time asked for)."
    )
    current: int = Field(
        description="Read already by the current extractor, in their current content."
    )
    enqueued: int = Field(description="Given an extract job; in a dry run, those that would be.")
    queued: int = Field(
        description="An extract job of their current content was queued or running already."
    )
    skipped: int = Field(
        description="Of a source no LLM may read (policy none), or that no configured LLM"
        " provider may serve."
    )

    @classmethod
    def of(cls, counts: ReextractionCounts) -> "ExtractCounts":
        return cls(
            records=counts.records,
            current=counts.current,
            enqueued=counts.enqueued,
            queued=counts.queued,
            skipped=counts.skipped,
        )


class SourceExtraction(ExtractCounts):
    source: str
    extractor: str | None = Field(
        description="The model and prompt version its jobs read with now, e.g."
        " qwen3:4b-instruct@decisions-v1; null when the source is skipped."
    )
    skipped_reason: str | None = Field(
        description="policy_none (no LLM may read the source or the workspace) or no_provider"
        " (no configured provider may); null when the source is not skipped."
    )

    @classmethod
    def of_source(cls, source: SourceReextraction) -> "SourceExtraction":
        counts = source.counts
        return cls(
            source=source.name,
            extractor=source.extractor,
            skipped_reason=source.skipped,
            records=counts.records,
            current=counts.current,
            enqueued=counts.enqueued,
            queued=counts.queued,
            skipped=counts.skipped,
        )


class ExtractAccepted(BaseModel):
    workspace: str
    dry_run: bool
    since: datetime | None
    totals: ExtractCounts
    sources: list[SourceExtraction] = Field(description="By name.")

    @classmethod
    def of(cls, result: Reextraction, since: datetime | None) -> "ExtractAccepted":
        return cls(
            workspace=result.workspace,
            dry_run=result.dry_run,
            since=since,
            totals=ExtractCounts.of(result.totals),
            sources=[SourceExtraction.of_source(source) for source in result.sources],
        )


def build_extract_router() -> APIRouter:
    """The re-extraction route; see the module."""
    router = APIRouter(prefix="/workspaces/{workspace}", tags=["decisions"])

    @router.post("/extract", status_code=202, summary="Extract decisions from stored records")
    async def extract(
        workspace: str,
        access: AdminAccess,
        conn: Connection,
        request: Request,
        response: Response,
        body: ExtractRequest | None = None,
    ) -> ExtractAccepted:
        """Enqueue the decision extraction of every live thread and meeting of the workspace
        whose current content the current extractor of its source (the model its LLM policies
        allow, and the prompt version) has not read: records stored before an LLM provider was
        configured, and every record after the extractor changed. Records go oldest first. A
        source no LLM may read is skipped and counted. A record whose extraction is queued or
        running already gets no second job, so calling again enqueues nothing while the jobs
        wait. Items recorded by hand are never touched. 202 with the counts per source; 200 for
        a dry run, which enqueues nothing."""
        options = body or ExtractRequest()
        settings: Settings = request.app.state.settings
        with admin_errors():
            result = await enqueue_reextraction(
                conn,
                access,
                workspace,
                configured_providers(settings),
                sources=options.sources,
                since=options.since,
                dry_run=options.dry_run,
            )
        if options.dry_run:
            response.status_code = 200
        return ExtractAccepted.of(result, options.since)

    return router
