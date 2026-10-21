"""In-process job execution for the ``memory`` backend (single API process)."""

from __future__ import annotations

import asyncio

from research_assistant.jobs.executor import JobExecutor
from research_assistant.logging import get_logger

__all__ = ["InProcessJobRunner"]

logger = get_logger(__name__)


class InProcessJobRunner:
    """Runs jobs as asyncio tasks in the current event loop, at most ``concurrency`` at once."""

    def __init__(self, executor: JobExecutor, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._executor = executor
        self._semaphore = asyncio.Semaphore(concurrency)
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def pending(self) -> int:
        """Jobs submitted and not yet finished (running or waiting for a slot)."""
        return len(self._tasks)

    def submit(self, job_id: str) -> asyncio.Task[None]:
        if self._closed:
            raise RuntimeError("runner is closed")
        task = asyncio.create_task(self._run(job_id), name=f"research-job-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run(self, job_id: str) -> None:
        async with self._semaphore:
            await self._executor.execute(job_id)

    async def aclose(self, grace: float = 0.0) -> None:
        """Stop accepting jobs, wait up to ``grace`` seconds, then cancel the rest."""
        self._closed = True
        tasks = set(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace) if grace > 0 else (set(), tasks)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("runner_cancelled_jobs", count=len(pending))
        await asyncio.gather(*tasks, return_exceptions=True)
