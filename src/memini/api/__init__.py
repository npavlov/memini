"""The HTTP API: a FastAPI application built by :func:`create_app`."""

from memini.api.app import OPENAPI_URL, V1_PREFIX, create_app
from memini.api.errors import ApiError, ErrorResponse
from memini.api.health import DEFAULT_READINESS_TIMEOUT, ReadinessCheck
from memini.api.request_id import REQUEST_ID_HEADER, current_request_id, request_id_var

__all__ = [
    "DEFAULT_READINESS_TIMEOUT",
    "OPENAPI_URL",
    "REQUEST_ID_HEADER",
    "V1_PREFIX",
    "ApiError",
    "ErrorResponse",
    "ReadinessCheck",
    "create_app",
    "current_request_id",
    "request_id_var",
]
