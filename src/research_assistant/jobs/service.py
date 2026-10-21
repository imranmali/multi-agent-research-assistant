"""``JobService``: the facade the API uses to submit, read, list and cancel jobs."""

from __future__ import annotations

import builtins
import contextlib
from collections.abc import Awaitable, Callable, Sequence

from redis.asyncio import Redis

from research_assistant.config import Settings
from research_assistant.jobs.executor import JobExecutor, RunnerFactory
from research_assistant.jobs.queue import JobQueue, RedisJobQueue
from research_assistant.jobs.runner import InProcessJobRunner
from research_assistant.jobs.store import InMemoryJobStore, JobStore, RedisJobStore
from research_assistant.logging import get_logger
from research_assistant.metrics import JOBS_FINISHED, JOBS_SUBMITTED
from research_assistant.models import (
    EventType,
    JobStatus,
    ProgressEvent,
    ResearchJob,
    ResearchRequest,
)

__all__ = [
    "JobAlreadyFinishedError",
    "JobNotFoundError",
    "JobService",
    "build_job_service",
]

logger = get_logger(__name__)

Closer = Callable[[], Awaitable[None]]


class JobNotFoundError(LookupError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Job {job_id} not found")


class JobAlreadyFinishedError(RuntimeError):
    def __init__(self, job: ResearchJob) -> None:
        self.job = job
        super().__init__(f"Job {job.id} already finished with status {job.status.value}")


class JobService:
    """Coordinates the store with either a queue (Redis backend) or an in-process runner."""

    def __init__(
        self,
        store: JobStore,
        *,
        queue: JobQueue | None = None,
        runner: InProcessJobRunner | None = None,
        shutdown_grace_seconds: float = 0.0,
        closers: Sequence[Closer] = (),
    ) -> None:
        if (queue is None) == (runner is None):
            raise ValueError("exactly one of queue or runner must be provided")
        self.store = store
        self.queue = queue
        self.runner = runner
        self._grace = shutdown_grace_seconds
        self._closers = list(closers)

    async def submit(self, request: ResearchRequest) -> ResearchJob:
        job = ResearchJob(request=request)
        await self.store.create(job)
        await self.store.append_event(
            job.id, ProgressEvent(type=EventType.JOB_QUEUED, stage="job", message="Job queued")
        )
        if self.queue is not None:
            await self.queue.enqueue(job.id)
        else:
            assert self.runner is not None
            self.runner.submit(job.id)
        JOBS_SUBMITTED.inc()
        logger.info("job_submitted", job_id=job.id)
        return job

    async def get(self, job_id: str) -> ResearchJob | None:
        return await self.store.get(job_id)

    async def list(self, limit: int = 20, offset: int = 0) -> builtins.list[ResearchJob]:
        return await self.store.list(limit=limit, offset=offset)

    async def events(self, job_id: str, since: int = 0) -> builtins.list[ProgressEvent]:
        return await self.store.events(job_id, since)

    async def cancel(self, job_id: str) -> ResearchJob:
        """Cancel a queued job immediately, or flag a running one to stop at the next stage."""
        current = await self.store.get(job_id)
        if current is None:
            raise JobNotFoundError(job_id)
        if current.status.terminal:
            raise JobAlreadyFinishedError(current)
        job = await self.store.request_cancel(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        if job.status.terminal and job.status is not JobStatus.CANCELLED:
            raise JobAlreadyFinishedError(job)  # finished while we were cancelling
        if current.status is JobStatus.QUEUED and job.status is JobStatus.CANCELLED:
            await self.store.append_event(
                job_id,
                ProgressEvent(
                    type=EventType.JOB_CANCELLED, stage="job", message="Cancelled before start"
                ),
            )
            JOBS_FINISHED.labels(status=JobStatus.CANCELLED.value).inc()
        logger.info("job_cancel_requested", job_id=job_id, status=job.status.value)
        return job

    async def ping(self) -> bool:
        try:
            return await self.store.ping()
        except Exception:
            logger.exception("store_ping_error")
            return False

    async def aclose(self) -> None:
        if self.runner is not None:
            await self.runner.aclose(self._grace)
        if self.queue is not None:
            await self.queue.aclose()
        await self.store.aclose()
        for closer in self._closers:
            with contextlib.suppress(Exception):
                await closer()


def _default_runner_factory(settings: Settings) -> tuple[RunnerFactory, Closer]:
    from research_assistant.llm.factory import build_llm_client
    from research_assistant.orchestrator import Orchestrator

    llm = build_llm_client(settings)
    return (lambda: Orchestrator(llm, settings)), llm.aclose


def build_job_service(
    settings: Settings,
    *,
    redis: Redis | None = None,
    runner_factory: RunnerFactory | None = None,
) -> JobService:
    """Wire the backend selected by ``settings.job_backend``.

    * ``memory``: in-memory store + in-process runner executing jobs in this event loop.
    * ``redis``: shared Redis store + queue; ``research-worker`` processes execute jobs.
    """
    if settings.job_backend == "redis":
        client = redis if redis is not None else Redis.from_url(settings.redis_url)
        closers: list[Closer] = [client.aclose] if redis is None else []
        return JobService(
            RedisJobStore(client, settings.job_ttl_seconds),
            queue=RedisJobQueue(client, settings.queue_name, consumer_id="api"),
            closers=closers,
        )

    store = InMemoryJobStore()
    closers = []
    if runner_factory is None:
        runner_factory, close_llm = _default_runner_factory(settings)
        closers.append(close_llm)
    executor = JobExecutor(store, runner_factory)
    runner = InProcessJobRunner(executor, settings.worker_concurrency)
    return JobService(
        store,
        runner=runner,
        shutdown_grace_seconds=settings.worker_shutdown_grace_seconds,
        closers=closers,
    )
