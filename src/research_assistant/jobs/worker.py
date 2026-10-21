"""``research-worker``: consumes the Redis queue and executes jobs with bounded concurrency."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
import uuid

from redis.asyncio import Redis

from research_assistant.config import Settings, get_settings
from research_assistant.jobs.executor import JobExecutor
from research_assistant.jobs.queue import JobQueue, RecoverableJobQueue, RedisJobQueue
from research_assistant.jobs.store import JobStore, RedisJobStore
from research_assistant.logging import configure_logging, get_logger
from research_assistant.metrics import JOBS_RECOVERED
from research_assistant.models import EventType, JobStatus, ProgressEvent

__all__ = ["Worker", "default_consumer_id", "main", "run_worker"]

logger = get_logger(__name__)

METRICS_PORT_ENV = "WORKER_METRICS_PORT"
DEFAULT_METRICS_PORT = 9100


def default_consumer_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    """Dequeue loop: at most ``concurrency`` jobs in flight; graceful stop with a grace period."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        queue: JobQueue,
        executor: JobExecutor,
        *,
        concurrency: int | None = None,
        consumer_id: str | None = None,
        dequeue_timeout: float = 1.0,
    ) -> None:
        self.settings = settings
        self.store = store
        self.queue = queue
        self.executor = executor
        self.concurrency = concurrency or settings.worker_concurrency
        self.consumer_id = consumer_id or default_consumer_id()
        self.grace = settings.worker_shutdown_grace_seconds
        self.heartbeat_interval = settings.worker_heartbeat_seconds
        self._dequeue_timeout = dequeue_timeout
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._finished = asyncio.Event()
        self.processed = 0

    @property
    def active(self) -> int:
        return len(self._tasks)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Stop taking new jobs (safe to call from a signal handler)."""
        if not self._stop.is_set():
            logger.info("worker_stopping", consumer_id=self.consumer_id, active=self.active)
        self._stop.set()

    async def stop(self) -> None:
        """Request a stop and wait until :meth:`run` has drained (up to the grace period)."""
        self.request_stop()
        await self._finished.wait()

    async def run(self) -> None:
        logger.info("worker_started", consumer_id=self.consumer_id, concurrency=self.concurrency)
        self._finished.clear()
        stop_waiter = asyncio.create_task(self._stop.wait())
        maintenance: asyncio.Task[None] | None = None
        if isinstance(self.queue, RecoverableJobQueue):
            # Announce liveness before taking any job, so nobody recovers our first one.
            await self.queue.heartbeat(self._heartbeat_ttl)
            maintenance = asyncio.create_task(self._maintain(self.queue))
        try:
            while not self._stop.is_set():
                if len(self._tasks) >= self.concurrency:
                    await asyncio.wait(
                        {*self._tasks, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                    )
                    continue
                try:
                    job_id = await self.queue.dequeue(self._dequeue_timeout)
                except Exception:
                    logger.exception("worker_dequeue_error")
                    await asyncio.sleep(min(self._dequeue_timeout, 1.0))
                    continue
                if job_id is not None:
                    # Once dequeued the job is ours: process it even if a stop just arrived.
                    task = asyncio.create_task(self._process(job_id), name=f"job-{job_id}")
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            stop_waiter.cancel()
            await self._drain()
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
                if isinstance(self.queue, RecoverableJobQueue):
                    with contextlib.suppress(Exception):
                        await self.queue.clear_heartbeat()
            self._finished.set()
            logger.info("worker_stopped", consumer_id=self.consumer_id, processed=self.processed)

    @property
    def _heartbeat_ttl(self) -> float:
        # Three missed beats before peers treat this worker as dead.
        return self.heartbeat_interval * 3

    async def _maintain(self, queue: RecoverableJobQueue) -> None:
        """Heartbeat, and re-queue jobs held by workers that stopped heartbeating."""
        while True:
            try:
                await queue.heartbeat(self._heartbeat_ttl)
                recovered = await queue.recover_orphaned(self._requeue)
                if recovered:
                    logger.warning("worker_recovered_jobs", count=len(recovered), jobs=recovered)
            except Exception:
                logger.exception("worker_maintenance_error")
            await asyncio.sleep(self.heartbeat_interval)

    async def _requeue(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.status is not JobStatus.RUNNING:
            return  # queued: will run normally; finished/missing: executor skips it
        await self.store.update(
            job.model_copy(update={"status": JobStatus.QUEUED, "started_at": None})
        )
        await self.store.append_event(
            job_id,
            ProgressEvent(
                type=EventType.JOB_QUEUED,
                stage="job",
                message="Re-queued after the worker running it stopped responding",
            ),
        )
        JOBS_RECOVERED.inc()

    async def _process(self, job_id: str) -> None:
        try:
            await self.executor.execute(job_id)
        finally:
            self.processed += 1
            try:
                await asyncio.shield(self.queue.ack(job_id))
            except Exception:
                logger.exception("worker_ack_error", job_id=job_id)

    async def _drain(self) -> None:
        tasks = set(self._tasks)
        if not tasks:
            return
        logger.info("worker_draining", active=len(tasks), grace_seconds=self.grace)
        _, pending = (
            await asyncio.wait(tasks, timeout=self.grace)
            if self.grace > 0
            else (
                set(),
                tasks,
            )
        )
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("worker_cancelled_jobs", count=len(pending))
        await asyncio.gather(*tasks, return_exceptions=True)


def _metrics_port() -> int:
    raw = os.environ.get(METRICS_PORT_ENV, str(DEFAULT_METRICS_PORT))
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{METRICS_PORT_ENV} must be an integer, got {raw!r}") from exc


async def run_worker(
    settings: Settings,
    *,
    redis: Redis | None = None,
    metrics_port: int | None = None,
    install_signal_handlers: bool = True,
    dequeue_timeout: float = 1.0,
) -> Worker:
    """Wire Redis store/queue + orchestrator into a :class:`Worker` and run until stopped."""
    from research_assistant.llm.factory import build_llm_client
    from research_assistant.orchestrator import Orchestrator

    client = redis if redis is not None else Redis.from_url(settings.redis_url)
    store = RedisJobStore(client, settings.job_ttl_seconds)
    consumer_id = default_consumer_id()
    queue = RedisJobQueue(client, settings.queue_name, consumer_id)
    llm = build_llm_client(settings)
    executor = JobExecutor(store, lambda: Orchestrator(llm, settings))
    worker = Worker(
        settings, store, queue, executor, consumer_id=consumer_id, dequeue_timeout=dequeue_timeout
    )

    port = _metrics_port() if metrics_port is None else metrics_port
    if port > 0:
        from prometheus_client import start_http_server

        start_http_server(port)
        logger.info("worker_metrics_listening", port=port)

    loop = asyncio.get_running_loop()
    signals = (signal.SIGTERM, signal.SIGINT)
    if install_signal_handlers:
        for sig in signals:
            loop.add_signal_handler(sig, worker.request_stop)
    try:
        await worker.run()
    finally:
        if install_signal_handlers:
            for sig in signals:
                loop.remove_signal_handler(sig)
        with contextlib.suppress(Exception):
            await llm.aclose()
        if redis is None:
            await client.aclose()
    return worker


def main() -> None:
    """Console-script entry point (``research-worker``)."""
    settings = get_settings()
    configure_logging(settings)
    if settings.job_backend != "redis":
        print(
            "research-worker requires JOB_BACKEND=redis "
            f"(current: {settings.job_backend!r}); with the memory backend the API process "
            "executes jobs itself.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    asyncio.run(run_worker(settings))
