"""Structured logging: structlog on top of the standard library.

* JSON lines when ``settings.log_json`` is true (production), a readable console format
  otherwise (development, tests).
* Records emitted through the standard ``logging`` module (uvicorn, httpx, redis, ...)
  are rendered by the same pipeline, so every line has the same shape.
* Request/job correlation ids live in :mod:`contextvars` and are merged into every log
  line emitted while they are bound (see :func:`bind_context`).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from structlog.typing import Processor

from research_assistant.config import Settings

__all__ = [
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "get_logger",
]

_HANDLER_NAME = "research_assistant"

#: Third-party loggers that install their own handlers; we route them through ours.
_ROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Configure structlog and the stdlib root logger. Safe to call repeatedly."""
    shared = _shared_processors()
    renderer: Processor
    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
        final: list[Processor] = [
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ]
    else:
        out = stream or sys.stderr
        renderer = structlog.dev.ConsoleRenderer(colors=out.isatty())
        final = [structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=final)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    for name in _ROUTED_LOGGERS:
        routed = logging.getLogger(name)
        routed.handlers.clear()
        routed.propagate = True


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger (optionally pre-bound with ``initial`` values)."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger.bind(**initial) if initial else logger


def bind_context(**values: Any) -> None:
    """Bind correlation values (e.g. ``request_id``, ``job_id``) to the current context."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


@contextmanager
def bound_context(**values: Any) -> Iterator[None]:
    """Bind values for the duration of a ``with`` block, restoring the previous ones after."""
    with structlog.contextvars.bound_contextvars(**values):
        yield
