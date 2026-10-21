from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY

from research_assistant.jobs.executor import (
    INTERRUPTED_MESSAGE,
    JobCancelledError,
    JobExecutor,
    ResearchRunner,
)
from research_assistant.jobs.store import InMemoryJobStore
from research_assistant.models import EventType, JobStatus, ResearchJob
from tests.conftest import FakeRunner, make_job, wait_for


def finished_count(status: str) -> float:
    return REGISTRY.get_sample_value("research_jobs_total", {"status": status}) or 0.0


async def setup(runner: FakeRunner) -> tuple[InMemoryJobStore, JobExecutor, ResearchJob]:
    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)
    return store, JobExecutor(store, runner.factory), job


async def event_types(store: InMemoryJobStore, job_id: str) -> list[EventType]:
    return [e.type for e in await store.events(job_id)]


async def test_successful_lifecycle() -> None:
    runner = FakeRunner(stages=["planning", "writing"])
    store, executor, job = await setup(runner)
    before = finished_count("completed")
    final = await executor.execute(job.id)
    assert final is not None
    assert final.status is JobStatus.COMPLETED
    assert final.started_at is not None
    assert final.finished_at is not None
    assert final.started_at <= final.finished_at
    assert final.report is not None
    assert final.report.sources
    assert final.error is None
    assert await store.get(job.id) == final
    assert await event_types(store, job.id) == [
        EventType.JOB_STARTED,
        EventType.STAGE_STARTED,
        EventType.STAGE_COMPLETED,
        EventType.STAGE_STARTED,
        EventType.STAGE_COMPLETED,
        EventType.JOB_COMPLETED,
    ]
    assert finished_count("completed") == before + 1
    assert REGISTRY.get_sample_value("research_jobs_running") == 0
    assert (
        REGISTRY.get_sample_value("research_job_duration_seconds_count", {"status": "completed"})
        or 0
    ) >= 1


async def test_pipeline_error_marks_failed_with_message() -> None:
    runner = FakeRunner(error=RuntimeError("No evidence could be gathered"))
    store, executor, job = await setup(runner)
    final = await executor.execute(job.id)
    assert final is not None
    assert final.status is JobStatus.FAILED
    assert final.error == "No evidence could be gathered"
    assert final.finished_at is not None
    assert final.report is None
    events = await store.events(job.id)
    assert events[-1].type is EventType.JOB_FAILED
    assert events[-1].data == {"error": "No evidence could be gathered"}


async def test_error_without_message_is_still_readable() -> None:
    _, executor, job = await setup(FakeRunner(error=ValueError()))
    final = await executor.execute(job.id)
    assert final is not None
    assert final.error == "ValueError raised without a message"


async def test_runner_factory_error_marks_failed() -> None:
    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)

    def broken_factory() -> ResearchRunner:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    final = await JobExecutor(store, broken_factory).execute(job.id)
    assert final is not None
    assert final.status is JobStatus.FAILED
    assert final.error == "ANTHROPIC_API_KEY is not set"


async def test_missing_job_is_skipped() -> None:
    runner = FakeRunner()
    executor = JobExecutor(InMemoryJobStore(), runner.factory)
    assert await executor.execute("missing") is None
    assert runner.calls == 0


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.RUNNING]
)
async def test_non_queued_job_is_skipped(status: JobStatus) -> None:
    runner = FakeRunner()
    store, executor, job = await setup(runner)
    await store.update(job.model_copy(update={"status": status}))
    final = await executor.execute(job.id)
    assert final is not None
    assert final.status is status
    assert runner.calls == 0
    assert await store.events(job.id) == []


async def test_cancelled_before_pickup_is_not_executed() -> None:
    runner = FakeRunner()
    store, executor, job = await setup(runner)
    await store.request_cancel(job.id)
    final = await executor.execute(job.id)
    assert final is not None
    assert final.status is JobStatus.CANCELLED
    assert runner.calls == 0


async def test_cancel_flag_on_queued_job_marks_cancelled() -> None:
    runner = FakeRunner()
    store, executor, job = await setup(runner)
    await store.update(job.model_copy(update={"cancel_requested": True}))
    final = await executor.execute(job.id)
    assert final is not None
    assert final.status is JobStatus.CANCELLED
    assert final.finished_at is not None
    assert runner.calls == 0
    assert await event_types(store, job.id) == [EventType.JOB_CANCELLED]


async def test_cancel_during_run_stops_at_next_stage() -> None:
    gate = asyncio.Event()
    runner = FakeRunner(stages=["planning", "research:1", "writing"], gate=gate)
    store, executor, job = await setup(runner)
    task = asyncio.create_task(executor.execute(job.id))
    await asyncio.wait_for(runner.started.wait(), 1)
    assert (await store.get(job.id)).status is JobStatus.RUNNING  # type: ignore[union-attr]
    await store.request_cancel(job.id)
    gate.set()
    final = await asyncio.wait_for(task, 1)
    assert final is not None
    assert final.status is JobStatus.CANCELLED
    assert final.report is None
    types = await event_types(store, job.id)
    assert types[-1] is EventType.JOB_CANCELLED
    # Stopped at the boundary: "writing" never started.
    assert [e.stage for e in await store.events(job.id)].count("writing") == 0


async def test_cancel_is_honoured_even_if_pipeline_swallows_it() -> None:
    gate = asyncio.Event()
    runner = FakeRunner(gate=gate, swallow=True)
    store, executor, job = await setup(runner)
    task = asyncio.create_task(executor.execute(job.id))
    await asyncio.wait_for(runner.started.wait(), 1)
    await store.request_cancel(job.id)
    gate.set()
    final = await asyncio.wait_for(task, 1)
    assert final is not None
    assert final.status is JobStatus.CANCELLED


async def test_cancel_wrapped_in_other_error_is_cancelled() -> None:
    class WrappingRunner:
        async def run(self, request: object, on_event: object = None) -> object:
            try:
                raise JobCancelledError("stop")
            except JobCancelledError as exc:
                raise RuntimeError("wrapped") from exc

    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)
    final = await JobExecutor(store, WrappingRunner).execute(job.id)  # type: ignore[arg-type]
    assert final is not None
    assert final.status is JobStatus.CANCELLED


async def test_cancel_racing_with_start_wins() -> None:
    class RacyStore(InMemoryJobStore):
        """Cancellation lands between the executor's read and its RUNNING write."""

        async def get(self, job_id: str) -> ResearchJob | None:
            job = await super().get(job_id)
            if job is not None and job.status is JobStatus.QUEUED:
                await self.request_cancel(job_id)
            return job

    runner = FakeRunner()
    store = RacyStore()
    job = make_job()
    await store.create(job)
    final = await JobExecutor(store, runner.factory).execute(job.id)
    assert final is not None
    assert final.status is JobStatus.CANCELLED
    assert runner.calls == 0


async def test_store_failure_never_escapes() -> None:
    class BrokenStore(InMemoryJobStore):
        async def get(self, job_id: str) -> ResearchJob | None:
            raise ConnectionError("redis down")

    executor = JobExecutor(BrokenStore(), FakeRunner().factory)
    assert await executor.execute("any") is None


async def test_task_cancellation_marks_job_interrupted() -> None:
    runner = FakeRunner(gate=asyncio.Event())  # never released
    store, executor, job = await setup(runner)
    task = asyncio.create_task(executor.execute(job.id))
    await asyncio.wait_for(runner.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def failed() -> bool:
        current = await store.get(job.id)
        return current is not None and current.status is JobStatus.FAILED

    await wait_for(failed)
    final = await store.get(job.id)
    assert final is not None
    assert final.error == INTERRUPTED_MESSAGE
    assert REGISTRY.get_sample_value("research_jobs_running") == 0
