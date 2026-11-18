"""One error shape for every error the HTTP API returns.

Every error response is JSON of this shape, whatever produced it: an :class:`ApiError` or another
``HTTPException`` raised by a route, a 404 or 405 from routing, a request that fails validation, or
an unhandled exception::

    {"error": {"code": "not_found", "message": "Not Found", "request_id": "…", "details": []}}

- ``code`` is a stable, machine-readable string; clients branch on it, not on ``message``.
- ``message`` is for people. It never holds request input or exception text.
- ``request_id`` matches the ``X-Request-ID`` response header.
- ``details`` lists the fields that failed validation, by location and error type only. It is empty
  for other errors. Request input values are never echoed: a body can hold private text or a token.

A 500 never says more than "internal error"; the exception goes to the server log. A database
statement or transaction of the request stopped by its time limit (:mod:`memini.db.limits`) answers
503 ``query_timeout``; search reports its own limit as 422 ``search_timeout`` before it gets here.
A database that cannot be reached, or whose session ended under the request (a restart, a crash,
no free connection in the pool within its timeout: :class:`psycopg.OperationalError`), answers 503
``unavailable``, as a service without a database does: the request may be sent again once the
database is back, and the pool reconnects by itself.
"""

import http.client
import logging
from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

from memini.api.request_id import current_request_id, new_request_id
from memini.db import TIMEOUT_ERRORS

__all__ = [
    "ERROR_RESPONSES",
    "ApiError",
    "ErrorBody",
    "ErrorResponse",
    "FieldError",
    "error_response",
    "install_error_handlers",
]


class FieldError(BaseModel):
    """One field that failed validation. Never carries the value that was sent."""

    location: list[str | int] = Field(
        description='Where the field is, for example ["body", "name"] or ["query", "limit"].',
    )
    type: str = Field(description='The kind of failure, for example "missing" or "int_parsing".')


class ErrorBody(BaseModel):
    code: str = Field(description='Stable, machine-readable error code, for example "not_found".')
    message: str = Field(description="Human-readable summary. Never holds request input.")
    request_id: str = Field(description="The request id, as in the X-Request-ID response header.")
    details: list[FieldError] = Field(
        default_factory=list[FieldError],
        description="Fields that failed validation; empty for other errors.",
    )


class ErrorResponse(BaseModel):
    """The body of every error response."""

    error: ErrorBody


ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    "default": {"model": ErrorResponse, "description": "Error; every error has this shape."},
}
"""OpenAPI ``responses`` that document the error shape on every route."""


class ApiError(HTTPException):
    """An error with a specific ``code``. Raise it from a route or a dependency.

    ``message`` is sent to the client as is: never put request input, a secret or exception text in
    it. ``headers`` are added to the response, for example ``WWW-Authenticate`` on a 401.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code, detail=message, headers=headers)
        self.code = code


# Codes for the statuses a client is likely to meet. Others fall back to client_error or
# server_error. Not derived from the reason phrase: phrases differ between Python versions.
_CODES: Final[Mapping[int, str]] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    410: "gone",
    413: "content_too_large",
    415: "unsupported_media_type",
    422: "unprocessable_content",
    429: "too_many_requests",
    500: "internal_error",
    501: "not_implemented",
    503: "unavailable",
}

_VALIDATION_ERROR: Final = "validation_error"
_INTERNAL_ERROR: Final = "internal_error"
_QUERY_TIMEOUT: Final = "query_timeout"
_UNAVAILABLE: Final = "unavailable"

_log = logging.getLogger(__name__)


def _code_for(status_code: int) -> str:
    code = _CODES.get(status_code)
    if code is not None:
        return code
    return "server_error" if status_code >= 500 else "client_error"


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Sequence[FieldError] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A response in the error shape, with the current request id."""
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            # The request-id middleware wraps every handler, so the id is always set here; the
            # fallback only keeps the shape whole should a handler run outside it.
            request_id=current_request_id() or new_request_id(),
            details=list(details),
        )
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, HTTPException):  # registered for HTTPException only
        raise exc
    if not is_body_allowed_for_status_code(exc.status_code):
        return Response(status_code=exc.status_code, headers=exc.headers)
    code = exc.code if isinstance(exc, ApiError) else _code_for(exc.status_code)
    # Typed str, but FastAPI's HTTPException passes any value through; only a non-empty string is
    # used as the message, anything else falls back to the reason phrase.
    detail = cast(object, exc.detail)
    message = (
        detail
        if isinstance(detail, str) and detail
        else http.client.responses.get(exc.status_code, "Error")
    )
    return error_response(exc.status_code, code, message, headers=exc.headers)


def _field_error(error: Any) -> FieldError:
    # Built from the location and the type only: an error's input, message and context can all
    # carry the value that was sent.
    loc: Sequence[Any] = error.get("loc", ())
    location = [part if isinstance(part, str | int) else str(part) for part in loc]
    return FieldError(location=location, type=str(error.get("type", "invalid")))


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # registered for it only
        raise exc
    details = [_field_error(error) for error in exc.errors()]
    return error_response(422, _VALIDATION_ERROR, "The request is not valid.", details=details)


async def _handle_database_timeout(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, TIMEOUT_ERRORS):  # registered for these only
        raise exc
    # The type only: the request's path and query can hold private text.
    _log.warning("request stopped by a database time limit (%s)", type(exc).__name__)
    return error_response(
        503,
        _QUERY_TIMEOUT,
        "A database statement of the request ran past its time limit and was stopped; try again"
        " later, or ask for less.",
    )


async def _handle_database_unavailable(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, psycopg.OperationalError):  # registered for it only
        raise exc
    # The type only: the message can name the server, and a pool's error its connection string.
    _log.warning("request failed: the database is not available (%s)", type(exc).__name__)
    return error_response(503, _UNAVAILABLE, "The database is not available; try again later.")


async def _handle_unexpected(request: Request, exc: Exception) -> Response:
    # Starlette re-raises the exception after this response is sent, so the server logs it.
    return error_response(500, _INTERNAL_ERROR, "Internal server error.")


def install_error_handlers(app: FastAPI) -> None:
    """Make every error ``app`` returns use the error shape."""
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    for timeout in TIMEOUT_ERRORS:
        app.add_exception_handler(timeout, _handle_database_timeout)
    # After the time limits, which are operational errors too: the most specific handler wins.
    app.add_exception_handler(psycopg.OperationalError, _handle_database_unavailable)
    app.add_exception_handler(Exception, _handle_unexpected)
