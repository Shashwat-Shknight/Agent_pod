"""One error shape for every API response, so the React client never has to guess.

Every failure is JSON: {"error": {"code": str, "message": str, "fields": {...}?}, "detail": str}
("detail" is kept so older callers and tests that read FastAPI's default key keep working).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db.client import StorageError

logger = logging.getLogger("nuucare.api")

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
    502: "upstream_failed",
    503: "service_unavailable",
    504: "upstream_timeout",
}


class ApiError(Exception):
    """Raise from any handler: ApiError(409, "conflict", "This consultation was changed by someone else.")."""

    def __init__(self, status: int, code: str, message: str, fields: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields or {}


def _body(code: str, message: str, fields: dict | None = None) -> dict:
    error = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return {"error": error, "detail": message}


def install_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(ApiError)
    async def api_error(_request: Request, error: ApiError):
        return JSONResponse(status_code=error.status, content=_body(error.code, error.message, error.fields))

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        message = error.detail if isinstance(error.detail, str) else "Request failed."
        content = _body(_STATUS_CODES.get(error.status_code, f"http_{error.status_code}"), message)
        if not isinstance(error.detail, str):
            content["detail"] = error.detail
        return JSONResponse(status_code=error.status_code, content=content, headers=getattr(error, "headers", None))

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        fields = {}
        for item in error.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path"))
            fields[location or "request"] = item.get("msg", "Invalid value")
        first = next(iter(fields.items()), ("request", "Invalid request"))
        message = f"{first[0]}: {first[1]}" if first[0] != "request" else first[1]
        return JSONResponse(status_code=422, content=_body("validation_failed", message, fields))

    @application.exception_handler(StorageError)
    async def storage_unavailable(_request: Request, error: StorageError):
        # Database outages are reported as 503 instead of an opaque 500; nothing falls back to local files.
        return JSONResponse(status_code=503, content=_body("database_unavailable", f"Database unavailable: {error}"))

    @application.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_body("internal_error", "Something went wrong on the server. The error has been logged."),
        )
