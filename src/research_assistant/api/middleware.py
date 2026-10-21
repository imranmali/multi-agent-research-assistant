"""Pure-ASGI middleware (safe for streaming responses such as SSE)."""

from __future__ import annotations

import json
import re
import time
import uuid

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_assistant.logging import bound_context, get_logger
from research_assistant.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS

__all__ = [
    "MAX_BODY_BYTES",
    "REQUEST_ID_HEADER",
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "RequestTooLargeError",
]

REQUEST_ID_HEADER = "X-Request-ID"
MAX_BODY_BYTES = 64 * 1024
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")

logger = get_logger("research_assistant.http")


def _error_payload(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode()


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    """Request id propagation, access logging, HTTP metrics and a last-resort 500 handler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        request_id = (
            incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        started = False
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, started
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with bound_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                logger.exception("unhandled_error", path=scope.get("path"))
                if not started:
                    await _send_json(
                        send_wrapper,
                        500,
                        _error_payload("internal_error", "Internal server error"),
                    )
                status_code = 500
            finally:
                elapsed = time.perf_counter() - start
                method = scope.get("method", "")
                route = _route_template(scope)
                HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
                HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(elapsed)
                logger.info(
                    "http_request",
                    method=method,
                    path=scope.get("path"),
                    route=route,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                )


class RequestTooLargeError(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"Request body exceeds {limit} bytes")


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` with 413.

    Declared ``Content-Length`` is checked up front; chunked bodies are counted while being
    read and abort with :class:`RequestTooLargeError` (rendered by the app's handlers).
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_json(
                send,
                413,
                _error_payload("payload_too_large", f"Request body exceeds {self.max_bytes} bytes"),
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestTooLargeError(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


async def _send_json(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
