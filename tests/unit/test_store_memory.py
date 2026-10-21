from __future__ import annotations

from datetime import timedelta

from research_assistant.jobs.store import InMemoryJobStore, JobStore
from research_assistant.models import EventType, JobStatus, ProgressEvent, utcnow
from tests.conftest import make_job


def event(stage: str) -> ProgressEvent:
    return ProgressEvent(type=EventType.STAGE_STARTED, stage=stage)


async def test_implements_protocol() -> None:
    assert isinstance(InMemoryJobStore(), JobStore)


async def test_create_get_roundtrip_returns_copies() -> None:
    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)
    loaded = await store.get(job.id)
    assert loaded == job
    assert loaded is not job
    loaded.error = "mutated"
    assert (await store.get(job.id)).error is None  # type: ignore[union-attr]
    assert await store.get("missing") is None


async def test_list_is_newest_first_with_offset() -> None:
    store = InMemoryJobStore()
    now = utcnow()
    jobs = [make_job(f"question {i}", created_at=now + timedelta(seconds=i)) for i in range(5)]
    for job in jobs:
        await store.create(job)
    listed = await store.list(limit=3)
    assert [j.id for j in listed] == [jobs[4].id, jobs[3].id, jobs[2].id]
    assert [j.id for j in await store.list(limit=10, offset=3)] == [jobs[1].id, jobs[0].id]
    assert await store.list(limit=5, offset=10) == []


async def test_events_are_indexed_and_filtered() -> None:
    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)
    assert await store.append_event(job.id, event("a")) == 0
    assert await store.append_event(job.id, event("b")) == 1
    assert await store.append_event(job.id, event("c")) == 2
    assert [e.stage for e in await store.events(job.id)] == ["a", "b", "c"]
    assert [e.stage for e in await store.events(job.id, since=1)] == ["b", "c"]
    assert await store.events(job.id, since=5) == []
    assert await store.events("unknown") == []


async def test_update_keeps_terminal_status_and_cancel_flag() -> None:
    store = InMemoryJobStore()
    job = make_job()
    await store.create(job)
    flagged = await store.update(job.model_copy(update={"cancel_requested": True}))
    assert flagged.cancel_requested
    # A stale writer without the flag cannot clear it.
    running = await store.update(job.model_copy(update={"status": JobStatus.RUNNING}))
    assert running.status is JobStatus.RUNNING
    assert running.cancel_requested
    done = await store.update(running.model_copy(update={"status": JobStatus.COMPLETED}))
    assert done.status is JobStatus.COMPLETED
    # Terminal states are final.
    back = await store.update(running.model_copy(update={"status": JobStatus.RUNNING}))
    assert back.status is JobStatus.COMPLETED
    assert (await store.get(job.id)).status is JobStatus.COMPLETED  # type: ignore[union-attr]


async def test_update_of_unknown_job_inserts_it() -> None:
    store = InMemoryJobStore()
    job = make_job()
    assert (await store.update(job)).id == job.id
    assert await store.get(job.id) is not None


async def test_request_cancel_transitions() -> None:
    store = InMemoryJobStore()
    queued, running, done = make_job(), make_job(), make_job()
    for job in (queued, running, done):
        await store.create(job)
    await store.update(running.model_copy(update={"status": JobStatus.RUNNING}))
    await store.update(done.model_copy(update={"status": JobStatus.COMPLETED}))

    cancelled = await store.request_cancel(queued.id)
    assert cancelled is not None
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.cancel_requested
    assert cancelled.finished_at is not None

    flagged = await store.request_cancel(running.id)
    assert flagged is not None
    assert flagged.status is JobStatus.RUNNING
    assert flagged.cancel_requested

    unchanged = await store.request_cancel(done.id)
    assert unchanged is not None
    assert unchanged.status is JobStatus.COMPLETED
    assert not unchanged.cancel_requested

    assert await store.request_cancel("missing") is None


async def test_ping_and_close() -> None:
    store = InMemoryJobStore()
    assert await store.ping()
    await store.aclose()
