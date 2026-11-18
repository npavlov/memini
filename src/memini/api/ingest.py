"""The push API: ``POST /v1/workspaces/{workspace}/sources/{source}/records``, the HTTP face of
:mod:`memini.ingest`.

The route needs the ``ingest`` scope; a token limited to one workspace stores nothing outside it
(another workspace answers 404, as one that does not exist). The body is one batch::

    {"stream": "chat/general",
     "window": {"from": "2026-03-01T00:00:00Z", "to": "2026-03-02T00:00:00Z", "complete": true},
     "records": [
       {"kind": "thread", "external_id": "general/1772323200.000100",
        "occurred_at": "2026-03-01T09:41:00Z",
        "external_url": "https://chat.example.invalid/archives/general/p1772323200000100",
        "payload": {"messages": [{"author": "Ann Example", "sent_at": "2026-03-01T09:30:00Z",
                                  "text": "Shall we move the release to Friday?"}]}},
       {"kind": "thread", "external_id": "general/1772236800.000200", "deleted": true}]}

Limits: the body (``ingest_max_body_size``) is refused by its Content-Length before it is read, or
as soon as more of it arrives: 413 ``content_too_large``. More records than
``ingest_max_records``: 413 ``too_many_records``. A payload over ``ingest_max_payload_size`` as
stored: 413 ``payload_too_large``. An object with a key twice in the body: 422 ``duplicate_key``.
A body not of the shape: 422 ``validation_error``, with the fields. A batch the contract refuses
(a payload without the fields of its kind, a window that ends in the future …): 422
``invalid_batch``, whose message names the field and why, never a value. A git source: 409
``not_ingestible``. A complete window that starts after the stream's checkpoint: 409
``window_gap``. Another batch in the way: 409 ``ingest_conflict``; sending the batch again is safe.
"""

import json
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from memini import ingest
from memini.api.auth import IngestAccess
from memini.api.database import Connection
from memini.api.errors import ApiError
from memini.config import Settings
from memini.db import Pool
from memini.workspaces import NotFoundError

__all__ = [
    "BatchBody",
    "CheckpointInfo",
    "IngestAccepted",
    "IngestCounts",
    "RecordBody",
    "WindowBody",
    "build_ingest_router",
]


# --- The body ------------------------------------------------------------------------------------


def _time_string(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("a time is a string")  # pydantic reports a ValueError as value_error
    return value


type _Time = Annotated[AwareDatetime, BeforeValidator(_time_string)]

_TIME = "An ISO 8601 time with a zone offset or Z, e.g. 2026-03-01T09:30:00Z."


class RecordBody(BaseModel):
    """A record to store, or with ``deleted`` a tombstone: only ``kind`` and ``external_id``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["thread", "meeting", "document"] = Field(
        description="thread: messages; meeting: a meeting with its notes; document: a text."
    )
    external_id: str = Field(
        min_length=1,
        description="The record's id in its source, as given: a thread key, a meeting id. At"
        f" most {ingest.MAX_EXTERNAL_ID_BYTES} bytes in UTF-8. With the kind and the source it"
        " makes the record's id, so the same record sent again is the same row.",
    )
    occurred_at: _Time | None = Field(
        default=None,
        description="When it happened in the source: the latest message of a thread, the start"
        " of a meeting. Required for a record; " + _TIME,
    )
    external_url: str | None = Field(
        default=None,
        max_length=ingest.MAX_EXTERNAL_URL_LENGTH,
        description="An http or https link to the record in its source.",
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description="The record as a JSON object, stored as canonical JSON (sorted keys, no"
        " whitespace); required for a record. thread: messages, a list of objects with author,"
        " sent_at and text. meeting: title and started_at, optionally ended_at. document: title"
        " and text. Other fields are kept.",
    )
    deleted: bool = Field(
        default=False,
        strict=True,
        description="true: the record was deleted at the source; send kind and external_id only.",
    )

    def to_record(self) -> ingest.RecordIn:
        return ingest.RecordIn(
            kind=self.kind,
            external_id=self.external_id,
            occurred_at=self.occurred_at,
            payload=self.payload,
            external_url=self.external_url,
            deleted=self.deleted,
        )


class WindowBody(BaseModel):
    """A claim that the batch covers ``[from, to)`` of its stream."""

    model_config = ConfigDict(extra="forbid")

    start: _Time = Field(alias="from", description="Where the window starts. " + _TIME)
    end: _Time = Field(alias="to", description="Where it ends, not included. " + _TIME)
    complete: bool = Field(
        strict=True,
        description="true: everything of the stream in the window is in the batch or was stored"
        " before, so the checkpoint may move to `to`. It must start at or before the"
        " checkpoint. false: the checkpoint stays.",
    )
    snapshot: bool = Field(
        default=False,
        strict=True,
        description="true (with complete): the batch holds every live record of the stream whose"
        " occurred_at is in the window; the stream's records in the window it leaves out are"
        " deleted.",
    )
    cursor: dict[str, Any] | None = Field(
        default=None,
        description="Where the next window starts, in the client's terms (a page token, a time);"
        " stored with the checkpoint when it moves and returned with it."
        f" At most {ingest.MAX_CURSOR_SIZE} bytes as JSON.",
    )

    def to_claim(self) -> ingest.WindowClaim:
        return ingest.WindowClaim(
            start=self.start,
            end=self.end,
            complete=self.complete,
            snapshot=self.snapshot,
            cursor=self.cursor,
        )


class BatchBody(BaseModel):
    """A batch of records for one source."""

    model_config = ConfigDict(extra="forbid")

    stream: str | None = Field(
        default=None,
        min_length=1,
        description="The stream the records come through (a channel, a mailbox query, a meeting"
        f" list), created on first use; at most {ingest.MAX_STREAM_KEY_BYTES} bytes in UTF-8."
        " Needed for a window.",
    )
    window: WindowBody | None = Field(default=None, description="A window of the stream.")
    records: list[RecordBody] = Field(
        default_factory=list[RecordBody],
        description="Records and tombstones; a kind and external id at most once. The most per"
        " batch is a setting of the service (ingest_max_records).",
    )

    def to_batch(self) -> ingest.Batch:
        return ingest.Batch(
            records=[record.to_record() for record in self.records],
            stream=self.stream,
            window=None if self.window is None else self.window.to_claim(),
        )


# --- The answer ----------------------------------------------------------------------------------


class IngestCounts(BaseModel):
    """Each item of the batch counts once, in inserted, updated, revived, deleted or unchanged."""

    inserted: int = Field(description="Records stored for the first time.")
    updated: int = Field(description="Records written again: another payload, time, URL or stream.")
    revived: int = Field(description="Deleted records sent again: live now.")
    deleted: int = Field(description="Live records the tombstones of the batch deleted.")
    unchanged: int = Field(
        description="Records equal to what was stored, and tombstones of records that were not"
        " live: nothing written."
    )
    reconciled: int = Field(
        description="Records of the stream in a snapshot's window that the snapshot left out:"
        " deleted now."
    )


class CheckpointInfo(BaseModel):
    checked_until: datetime | None = Field(
        description="Everything in the stream up to this time is stored; null before the first"
        " complete window."
    )
    cursor: dict[str, Any] | None = Field(
        description="The cursor of the window that set the checkpoint, if it had one."
    )
    moved: bool = Field(description="Whether this batch moved the checkpoint.")


class IngestAccepted(BaseModel):
    run_id: UUID | None = Field(description="The run of the batch; GET /v1/runs/{id} has it.")
    stream: str | None = Field(description="The stream's key; null without one.")
    window_complete: bool | None = Field(description="null without a window claim.")
    counts: IngestCounts
    checkpoint: CheckpointInfo | None = Field(
        description="The stream's checkpoint after the batch; null without a stream."
    )

    @classmethod
    def of(cls, result: ingest.IngestResult) -> "IngestAccepted":
        checkpoint = result.checkpoint
        return cls(
            run_id=result.run_id,
            stream=result.stream,
            window_complete=result.window_complete,
            counts=IngestCounts(
                inserted=result.inserted,
                updated=result.updated,
                revived=result.revived,
                deleted=result.deleted,
                unchanged=result.unchanged,
                reconciled=result.reconciled,
            ),
            checkpoint=None
            if checkpoint is None
            else CheckpointInfo(
                checked_until=checkpoint.checked_until,
                cursor=None if checkpoint.cursor is None else dict(checkpoint.cursor),
                moved=result.checkpoint_moved,
            ),
        )


# --- Parsing and errors --------------------------------------------------------------------------


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApiError(422, "duplicate_key", "An object in the body has the same key twice.")
        result[key] = value
    return result


def _content_too_large(limit: int) -> ApiError:
    return ApiError(
        413,
        "content_too_large",
        f"The body is larger than {limit} bytes; send the records in smaller batches.",
    )


class _BatchRequest(Request):
    """The request of a batch: its body read up to the size limit, its JSON parsed strictly.

    A body over ``ingest_max_body_size`` is refused by its Content-Length before any of it is
    read, and otherwise as soon as more of it arrives. An object with a key twice is refused: the
    parser would keep the last value and drop the others without a word, and the stored payload
    would not be what was sent.
    """

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            limit = cast(Settings, self.app.state.settings).ingest_max_body_size
            declared = self.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise _content_too_large(limit)
            chunks: list[bytes] = []
            size = 0
            async for chunk in self.stream():
                size += len(chunk)
                if size > limit:
                    raise _content_too_large(limit)
                chunks.append(chunk)
            self._body = b"".join(chunks)
        return self._body

    async def json(self) -> Any:
        if not hasattr(self, "_json"):
            body = await self.body()
            try:
                self._json = json.loads(body, object_pairs_hook=_unique_keys)
            except json.JSONDecodeError:
                raise  # FastAPI answers 422 with the position
            except (ValueError, RecursionError):  # not UTF-8, a number too long, too deep
                raise ApiError(
                    422, "invalid_json", "The body is not JSON this API reads."
                ) from None
        return self._json


class _BatchRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def batch(request: Request) -> Response:
            return await handler(_BatchRequest(request.scope, request.receive))

        return batch


@contextmanager
def _ingest_errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as error:
        raise ApiError(404, "not_found", f"No such {error.what}.") from None
    except ingest.BatchTooLargeError as error:
        raise ApiError(413, error.code, f"The batch is too large: {error}.") from None
    except ingest.InvalidBatchError as error:
        raise ApiError(422, "invalid_batch", f"The batch is not valid: {error}.") from None
    except ingest.NotIngestibleError:
        raise ApiError(
            409,
            "not_ingestible",
            "Records of this source come from its repository; it takes no batches.",
        ) from None
    except ingest.WindowGapError as error:
        raise ApiError(409, "window_gap", f"The window leaves a gap: {error}.") from None
    except (ingest.StreamChangedError, ingest.BatchConflictError):
        raise ApiError(
            409,
            "ingest_conflict",
            "Another batch changed the same stream or records; send the batch again.",
        ) from None


def build_ingest_router() -> APIRouter:
    """The ingest route; see the module."""
    router = APIRouter(
        prefix="/workspaces/{workspace}/sources", tags=["ingest"], route_class=_BatchRoute
    )

    @router.post("/{source}/records", summary="Ingest a batch of records")
    async def ingest_records(
        workspace: str,
        source: str,
        body: BatchBody,
        access: IngestAccess,
        request: Request,
        conn: Connection,
    ) -> IngestAccepted:
        """Store a batch of records for a push source in one transaction, with the move of its
        stream's checkpoint when it claims a complete window. Sending the same batch again changes
        nothing. The answer counts what the batch did and gives the stream's checkpoint."""
        settings = cast(Settings, request.app.state.settings)
        pool = cast(Pool | None, request.app.state.pool)
        with _ingest_errors():
            result = await ingest.ingest(
                conn,
                access,
                workspace,
                source,
                body.to_batch(),
                limits=ingest.IngestLimits.from_settings(settings),
                runs=pool,
            )
        return IngestAccepted.of(result)

    return router
