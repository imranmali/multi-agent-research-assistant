"""FastAPI application factory.

Run with ``uvicorn --factory research_assistant.api.app:create_app`` (or
``research-assistant serve``). There is deliberately no module-level ``app``: settings come
from the environment when the factory is called, not at import time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from research_assistant import __version__
from research_assistant.api.middleware import (
    MAX_BODY_BYTES,
    REQUEST_ID_HEADER,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from research_assistant.api.routes import NotFoundError, ops_router, research_router
from research_assistant.config import Settings, get_settings
from research_assistant.jobs.service import JobService, build_job_service
from research_assistant.logging import configure_logging, get_logger

__all__ = ["create_app"]

logger = get_logger(__name__)

_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "too_many_requests",
    503: "service_unavailable",
}


def _error(status_code: int, message: str, *, code: str | None = None) -> dict[str, object]:
    if code is None:
        code = _CODES.get(status_code) or HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    return {"error": {"code": code, "message": message}}


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return JSONResponse(
        _error(exc.status_code, message), status_code=exc.status_code, headers=exc.headers
    )


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    body = _error(422, "Request validation failed")
    body["error"]["details"] = jsonable_encoder(exc.errors())  # type: ignore[index]
    return JSONResponse(body, status_code=422)


async def _not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(_error(404, str(exc)), status_code=404)


def create_app(settings: Settings | None = None, *, service: JobService | None = None) -> FastAPI:
    """Build the API. Inject ``service`` in tests; otherwise it is built from ``settings``."""
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = service is None
        app.state.job_service = service or build_job_service(settings)
        logger.info("api_started", job_backend=settings.job_backend, version=__version__)
        try:
            yield
        finally:
            if owned:
                await app.state.job_service.aclose()
                app.state.job_service = None
            logger.info("api_stopped")

    app = FastAPI(
        title="Multi-Agent Research Assistant API",
        version=__version__,
        description=(
            "Submit research questions, stream progress and fetch cited reports produced "
            "by a team of Claude-powered agents."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.job_service = service

    app.include_router(research_router)
    app.include_router(ops_router)

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(NotFoundError, _not_found_handler)

    # Added inner-most first: CORS -> body limit -> request context (outer-most).
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key", REQUEST_ID_HEADER, "Last-Event-ID"],
            expose_headers=[REQUEST_ID_HEADER, "Location"],
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(RequestContextMiddleware)
    return app
