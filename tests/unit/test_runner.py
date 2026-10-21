from __future__ import annotations

import asyncio

import pytest

from research_assistant.jobs.executor import INTERRUPTED_MESSAGE, JobExecutor
from research_assistant.jobs.runner import InProcessJobRunner
from research_assistant.jobs.store import InMemoryJobStore
from research_assistant.models import JobStatus
from tests.conftest import FakeRunner, make_job


async def make(
    runner: FakeRunner, concurrency: int, jobs: int
) -> tuple[InMemoryJobStore, InProcessJobRunner, list[str]]:
    store = InMemoryJobStore()
    ids = []
    for i in range(jobs):
        job = make_job(f"question {i}")
        await store.create(job)
        ids.append(job.id)
    return store, InProcessJobRunner(JobExecutor(store, runner.factory), concurrency), ids


async def test_runs_jobs_with_bounded_concurrency() -> None:
    fake = FakeRunner(delay=0.01)
    store, runner, ids = await make(fake, concurrency=2, jobs=5)
    tasks = [runner.submit(i) for i in ids]
    assert runner.pending == 5
    await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert fake.max_active == 2
    assert fake.calls == 5
    for job_id in ids:
        assert (await store.get(job_id)).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    assert runner.pending == 0
    await runner.aclose()


async def test_rejects_invalid_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        InProcessJobRunner(JobExecutor(InMemoryJobStore(), FakeRunner().factory), 0)


async def test_aclose_waits_for_running_jobs_within_grace() -> None:
    fake = FakeRunner(delay=0.05)
    store, runner, ids = await make(fake, concurrency=1, jobs=1)
    runner.submit(ids[0])
    await asyncio.wait_for(fake.started.wait(), 1)
    await runner.aclose(grace=2)
    assert (await store.get(ids[0])).status is JobStatus.COMPLETED  # type: ignore[union-attr]
    with pytest.raises(RuntimeError, match="closed"):
        runner.submit("another")


async def test_aclose_cancels_jobs_after_grace() -> None:
    fake = FakeRunner(gate=asyncio.Event())  # never finishes on its own
    store, runner, ids = await make(fake, concurrency=1, jobs=1)
    runner.submit(ids[0])
    await asyncio.wait_for(fake.started.wait(), 1)
    await runner.aclose(grace=0.01)
    job = await store.get(ids[0])
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error == INTERRUPTED_MESSAGE


async def test_aclose_without_grace_cancels_immediately_and_is_idempotent() -> None:
    fake = FakeRunner(gate=asyncio.Event())
    _, runner, ids = await make(fake, concurrency=1, jobs=2)
    runner.submit(ids[0])
    runner.submit(ids[1])
    await asyncio.wait_for(fake.started.wait(), 1)
    await runner.aclose()
    assert runner.pending == 0
    await runner.aclose()
