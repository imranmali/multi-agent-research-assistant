"""Executes one research job: lifecycle transitions, progress events, cooperative cancel."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from research_assistant.jobs.store import JobStore
from research_assistant.logging import bound_context, get_logger
from research_assistant.metrics import JOB_DURATION, JOBS_FINISHED, JOBS_RUNNING
from research_assistant.models import (
    EventType,
    JobStatus,
    ProgressEvent,
    ResearchJob,
    ResearchReport,
    ResearchRequest,
    utcnow,
)

__all__ = [
    "EventCallback",
    "JobCancelledError",
    "JobExecutor",
    "ResearchRunner",
    "RunnerFactory",
]

EventCallback = Callable[[ProgressEvent], Awaitable[None]]

logger = get_logger(__name__)

JOB_STAGE = "job"
INTERRUPTED_MESSAGE = "Job was interrupted before it finished (executor shut down)."


class ResearchRunner(Protocol):
    """What the executor needs from the research pipeline (the ``Orchestrator``)."""

    async def run(
        self, request: ResearchRequest, on_event: EventCallback | None = None
    ) -> ResearchReport: ...


RunnerFactory = Callable[[], ResearchRunner]


class JobCancelledError(Exception):
    """Raised from the progress hook to stop a job whose cancellation was requested."""


class JobExecutor:
    """Runs jobs taken from the queue (or the in-process runner) to a terminal state."""

    def __init__(self, store: JobStore, runner_factory: RunnerFactory) -> None:
        self._store = store
        self._runner_factory = runner_factory

    async def execute(self, job_id: str) -> ResearchJob | None:
        """Execute ``job_id``. Never raises (except task cancellation); returns the final job."""
        with bound_context(job_id=job_id):
            try:
                return await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job_execution_error")
                return None

    async def _execute(self, job_id: str) -> ResearchJob | None:
        job = await self._store.get(job_id)
        if job is None:
            logger.warning("job_skipped", reason="not_found")
            return None
        if job.status is not JobStatus.QUEUED:
            logger.info("job_skipped", reason="not_queued", status=job.status.value)
            return job
        if job.cancel_requested:
            return await self._finish(job, JobStatus.CANCELLED, message="Cancelled before start")

        job = await self._store.update(
            job.model_copy(update={"status": JobStatus.RUNNING, "started_at": utcnow()})
        )
        if job.status is not JobStatus.RUNNING:  # cancelled between read and write
            logger.info("job_skipped", reason="cancelled", status=job.status.value)
            return job
        await self._emit(job.id, EventType.JOB_STARTED, "Job started")
        logger.info("job_started")

        cancel_signalled = False

        async def on_event(event: ProgressEvent) -> None:
            nonlocal cancel_signalled
            await self._store.append_event(job_id, event)
            current = await self._store.get(job_id)
            if current is not None and current.cancel_requested:
                cancel_signalled = True
                raise JobCancelledError(f"Job {job_id} was cancelled")

        JOBS_RUNNING.inc()
        started = time.monotonic()
        final = job
        try:
            runner = self._runner_factory()
            report = await runner.run(job.request, on_event)
        except asyncio.CancelledError:
            final = await asyncio.shield(
                self._finish(job, JobStatus.FAILED, error=INTERRUPTED_MESSAGE)
            )
            raise
        except Exception as exc:
            if cancel_signalled or _caused_by_cancel(exc):
                final = await self._finish(job, JobStatus.CANCELLED, message="Job cancelled")
            else:
                logger.warning("job_failed", error=_describe(exc), exc_info=exc)
                final = await self._finish(job, JobStatus.FAILED, error=_describe(exc))
        else:
            if cancel_signalled:
                # The pipeline swallowed the cancellation signal; honour the request anyway.
                final = await self._finish(job, JobStatus.CANCELLED, message="Job cancelled")
            else:
                final = await self._finish(job, JobStatus.COMPLETED, report=report)
        finally:
            JOBS_RUNNING.dec()
            JOB_DURATION.labels(status=final.status.value).observe(time.monotonic() - started)
        return final

    async def _finish(
        self,
        job: ResearchJob,
        status: JobStatus,
        *,
        error: str | None = None,
        report: ResearchReport | None = None,
        message: str = "",
    ) -> ResearchJob:
        final = await self._store.update(
            job.model_copy(
                update={
                    "status": status,
                    "finished_at": utcnow(),
                    "error": error,
                    "report": report,
                }
            )
        )
        event_type = {
            JobStatus.COMPLETED: EventType.JOB_COMPLETED,
            JobStatus.FAILED: EventType.JOB_FAILED,
            JobStatus.CANCELLED: EventType.JOB_CANCELLED,
        }[status]
        default_message = {
            JobStatus.COMPLETED: "Job completed",
            JobStatus.FAILED: f"Job failed: {error}",
            JobStatus.CANCELLED: "Job cancelled",
        }[status]
        data: dict[str, object] = {"error": error} if error else {}
        await self._emit(job.id, event_type, message or default_message, data)
        JOBS_FINISHED.labels(status=status.value).inc()
        logger.info("job_finished", status=status.value, error=error)
        return final

    async def _emit(
        self,
        job_id: str,
        event_type: EventType,
        message: str,
        data: dict[str, object] | None = None,
    ) -> None:
        await self._store.append_event(
            job_id,
            ProgressEvent(type=event_type, stage=JOB_STAGE, message=message, data=data or {}),
        )


def _caused_by_cancel(exc: BaseException) -> bool:
    """Whether ``exc`` is, or was raised while handling, a :class:`JobCancelledError`."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, JobCancelledError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _describe(exc: BaseException) -> str:
    """Human-readable error for API clients."""
    text = str(exc).strip()
    return text or f"{type(exc).__name__} raised without a message"
