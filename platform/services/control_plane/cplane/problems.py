"""RFC 9457 problem documents for /v1 (plan P1.6).

Legacy routes keep FastAPI's ``{"detail": ...}`` shape, which every current client parses; /v1
clients get a typed error (``type``, ``title``, ``status``, ``detail``, ``instance``, and ``errors``
for validation failures) served as ``application/problem+json``.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler as _default_http_handler
from fastapi.exception_handlers import (
    request_validation_exception_handler as _default_validation_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

PROBLEM_PREFIX = "/v1/"


def problem(request: Request, status_code: int, detail: Any, headers: Any = None) -> JSONResponse:
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "instance": request.url.path,
    }
    if isinstance(detail, str):
        body["detail"] = detail
    else:
        body["detail"] = title
        body["errors"] = detail
    return JSONResponse(
        body, status_code=status_code, headers=headers, media_type="application/problem+json"
    )


async def _http_problem_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if request.url.path.startswith(PROBLEM_PREFIX):
        return problem(request, exc.status_code, exc.detail, getattr(exc, "headers", None))
    return await _default_http_handler(request, exc)


async def _validation_problem_handler(request: Request, exc: RequestValidationError) -> Response:
    if request.url.path.startswith(PROBLEM_PREFIX):
        return problem(request, 422, jsonable_encoder(exc.errors()))
    return await _default_validation_handler(request, exc)


def install(app: FastAPI) -> None:
    """Register the /v1 problem handlers on ``app`` (legacy routes fall through unchanged)."""
    app.add_exception_handler(StarletteHTTPException, _http_problem_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_problem_handler)  # type: ignore[arg-type]
