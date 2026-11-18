"""Request ids: one per HTTP request, in the ``X-Request-ID`` response header and in error bodies.

An incoming ``X-Request-ID`` is kept when it is 1 to 64 characters from ``A-Z a-z 0-9 . _ -``;
anything else is replaced by a fresh id, so a client cannot put control characters or long text into
logs through it. While a request is handled, its id is in :data:`request_id_var`; logging reads it
from there. The variable is defined in :mod:`memini.logs`, which must not import the HTTP
application, and is re-exported here.
"""

import re
import uuid
from typing import Final

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memini.logs import request_id_var

__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIdMiddleware",
    "accept_request_id",
    "current_request_id",
    "new_request_id",
    "request_id_var",
]

REQUEST_ID_HEADER: Final = "X-Request-ID"

_VALID_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")


def current_request_id() -> str | None:
    """The id of the HTTP request being handled, or ``None`` outside a request."""
    return request_id_var.get()


def new_request_id() -> str:
    """A fresh request id: 32 lowercase hex characters."""
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str | None:
    """``value`` if it is safe to use as a request id, else ``None``."""
    if value is not None and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return None


class RequestIdMiddleware:
    """Assign a request id, expose it in :data:`request_id_var` and the response header.

    It must wrap everything else, including the handler that turns an unhandled exception into a
    500, so that every response carries the header and every error body can read the id.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = accept_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER)) or (
            new_request_id()
        )

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
