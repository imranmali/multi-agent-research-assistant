"""Prometheus metrics shared by the API and the job layer.

Metrics live in the default registry so ``/metrics`` and the worker's metrics server expose
them without extra wiring. Creation goes through :func:`_get_or_create`, which returns the
already-registered collector when a module is imported twice (e.g. by test tooling), instead
of failing with a duplicate-registration error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.registry import CollectorRegistry

__all__ = [
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "JOBS_FINISHED",
    "JOBS_RECOVERED",
    "JOBS_RUNNING",
    "JOBS_SUBMITTED",
    "JOB_DURATION",
]

_C = TypeVar("_C", Counter, Gauge, Histogram)


def _get_or_create(
    cls: type[_C],
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    *,
    registry: CollectorRegistry = REGISTRY,
    **kwargs: Any,
) -> _C:
    try:
        return cls(name, documentation, labelnames, registry=registry, **kwargs)
    except ValueError:
        # Already registered (module re-imported): reuse the existing collector.
        names = registry._names_to_collectors
        existing = names.get(name) or names.get(f"{name}_total")
        if isinstance(existing, cls):
            return existing
        raise


JOBS_SUBMITTED = _get_or_create(
    Counter, "research_jobs_submitted", "Research jobs accepted by the API."
)
JOBS_RECOVERED = _get_or_create(
    Counter,
    "research_jobs_recovered",
    "Research jobs re-queued after the worker running them stopped heartbeating.",
)
JOBS_FINISHED = _get_or_create(
    Counter,
    "research_jobs",
    "Research jobs that reached a final state, by status.",
    ("status",),
)
JOB_DURATION = _get_or_create(
    Histogram,
    "research_job_duration_seconds",
    "Wall-clock duration of executed research jobs, by final status.",
    ("status",),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600),
)
JOBS_RUNNING = _get_or_create(
    Gauge, "research_jobs_running", "Research jobs currently executing in this process."
)
HTTP_REQUESTS = _get_or_create(
    Counter,
    "http_requests",
    "HTTP requests handled, by method, route template and status code.",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = _get_or_create(
    Histogram,
    "http_request_duration_seconds",
    "HTTP request latency, by method and route template.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
