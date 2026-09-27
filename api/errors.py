"""The standard error body (plan.MD §7.2):

    {"error_code": "...", "detail": ..., "retryable": false, "retry_after_s": null}

`detail` is a message string (or, for a few older answers, an object with
error_code/message); clients branch on `error_code`. FastAPI's own errors
(404 for an unknown route, 422 for a malformed body, HTTPException raised by
a route) get the same top-level fields added, so every error answer from the
API can be handled the same way."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.logging import get_logger
from services.errors import ServiceError

logger = get_logger(__name__)

CODES_BY_STATUS = {
    400: "bad_request", 401: "unauthorized", 402: "plan_limit", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 410: "gone", 413: "too_large", 415: "unsupported_media_type",
    422: "validation_error", 425: "too_early", 429: "rate_limited", 500: "internal", 502: "bad_gateway",
    503: "unavailable",
}


def error_body(code: str, detail, retryable: bool = False, retry_after_s: int | None = None) -> dict:
    return {"error_code": code, "retryable": retryable, "retry_after_s": retry_after_s, "detail": detail}


def error_response(status_code: int, code: str, detail, *, retryable: bool = False,
                   retry_after_s: int | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    merged = dict(headers or {})
    if retry_after_s:
        merged.setdefault("Retry-After", str(retry_after_s))
    return JSONResponse(status_code=status_code, content=error_body(code, detail, retryable, retry_after_s),
                        headers=merged or None)


def service_error_response(exc: ServiceError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return error_response(exc.status, exc.code, exc.message, retryable=exc.retryable,
                          retry_after_s=exc.retry_after_s, headers=headers)


# Pages (not API routes) answer errors with an HTML page; api/pages.py
# registers the renderer so this module doesn't depend on the templates.
HtmlErrorRenderer = Callable[[Request, int, str], Response | None]
_html_renderer: HtmlErrorRenderer | None = None


def set_html_error_renderer(renderer: HtmlErrorRenderer) -> None:
    global _html_renderer
    _html_renderer = renderer


def _wants_html(request: Request) -> bool:
    from .auth import is_api_path

    return (not is_api_path(request.url.path) and not request.url.path.startswith("/static/")
            and "text/html" in request.headers.get("accept", ""))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if _html_renderer is not None and _wants_html(request):
            page = _html_renderer(request, exc.status, exc.message)
            if page is not None:
                return page
        return service_error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _html_renderer is not None and _wants_html(request):
            page = _html_renderer(request, exc.status_code, str(exc.detail))
            if page is not None:
                return page
        detail = exc.detail
        code = detail.get("error_code") if isinstance(detail, dict) and detail.get("error_code") else \
            CODES_BY_STATUS.get(exc.status_code, "error")
        return JSONResponse(status_code=exc.status_code, content=error_body(code, jsonable_encoder(detail)),
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content=error_body("validation_error", jsonable_encoder(exc.errors())))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        if _html_renderer is not None and _wants_html(request):
            page = _html_renderer(request, 500, "")
            if page is not None:
                return page
        return JSONResponse(status_code=500, content=error_body("internal", "unexpected server error; it was logged"))
