from __future__ import annotations

import time
from datetime import timedelta

import fakeredis
import pytest
from redis.asyncio.client import Pipeline

from research_assistant.jobs.store import JobStore, RedisJobStore
from research_assistant.models import EventType, JobStatus, ProgressEvent, ResearchJob, utcnow
from tests.conftest import make_job

TTL = 3600


def event(stage: str) -> ProgressEvent:
    return ProgressEvent(type=EventType.STAGE_COMPLETED, stage=stage)


@pytest.fixture
def store(redis: fakeredis.FakeAsyncRedis) -> RedisJobStore:
    return RedisJobStore(redis, TTL)


async def test_implements_protocol(store: RedisJobStore) -> None:
    assert isinstance(store, JobStore)


async def test_roundtrip_and_ttl_on_every_key(
    store: RedisJobStore, redis: fakeredis.FakeAsyncRedis
) -> None:
    job = make_job()
    await store.create(job)
    await store.append_event(job.id, event("planning"))
    assert await store.get(job.id) == job
    assert await store.get("missing") is None
    for key in (f"job:{job.id}", f"job:{job.id}:events", "jobs:index"):
        ttl = await redis.ttl(key)
        assert 0 < ttl <= TTL, key


async def test_writes_refresh_ttl(store: RedisJobStore, redis: fakeredis.FakeAsyncRedis) -> None:
    job = make_job()
    await store.create(job)
    await store.append_event(job.id, event("a"))
    await redis.expire(f"job:{job.id}", 10)
    await redis.expire(f"job:{job.id}:events", 10)
    await store.update(job.model_copy(update={"status": JobStatus.RUNNING}))
    assert await redis.ttl(f"job:{job.id}") > 10
    assert await redis.ttl(f"job:{job.id}:events") > 10
    await redis.expire(f"job:{job.id}", 10)
    await store.append_event(job.id, event("b"))
    assert await redis.ttl(f"job:{job.id}") > 10


async def test_state_is_shared_between_instances(redis_server: fakeredis.FakeServer) -> None:
    client_a = fakeredis.FakeAsyncRedis(server=redis_server)
    client_b = fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)
    store_a, store_b = RedisJobStore(client_a, TTL), RedisJobStore(client_b, TTL)
    job = make_job()
    await store_a.create(job)
    await store_a.append_event(job.id, event("planning"))
    assert await store_b.get(job.id) == job
    assert [e.stage for e in await store_b.events(job.id)] == ["planning"]
    assert [j.id for j in await store_b.list()] == [job.id]
    cancelled = await store_b.request_cancel(job.id)
    assert cancelled is not None
    assert (await store_a.get(job.id)).status is JobStatus.CANCELLED  # type: ignore[union-attr]
    await client_a.aclose()
    await client_b.aclose()


async def test_events_since(store: RedisJobStore) -> None:
    job = make_job()
    await store.create(job)
    indexes = [await store.append_event(job.id, event(s)) for s in ("a", "b", "c")]
    assert indexes == [0, 1, 2]
    assert [e.stage for e in await store.events(job.id, since=2)] == ["c"]
    assert await store.events(job.id, since=3) == []


async def test_list_newest_first_and_trims_expired(
    store: RedisJobStore, redis: fakeredis.FakeAsyncRedis
) -> None:
    now = utcnow()
    jobs = [make_job(f"question {i}", created_at=now + timedelta(seconds=i)) for i in range(4)]
    for job in jobs:
        await store.create(job)
    assert [j.id for j in await store.list(limit=2)] == [jobs[3].id, jobs[2].id]
    assert [j.id for j in await store.list(limit=2, offset=2)] == [jobs[1].id, jobs[0].id]
    assert await store.list(limit=2, offset=10) == []

    # A job whose key expired and an index entry older than the retention window.
    await redis.delete(f"job:{jobs[3].id}")
    await redis.zadd("jobs:index", {"ancient": time.time() - TTL - 10})
    listed = await store.list(limit=10)
    assert [j.id for j in listed] == [jobs[2].id, jobs[1].id, jobs[0].id]
    members = {m.decode() for m in await redis.zrange("jobs:index", 0, -1)}
    assert members == {jobs[0].id, jobs[1].id, jobs[2].id}


async def test_update_invariants(store: RedisJobStore) -> None:
    job = make_job()
    await store.create(job)
    await store.request_cancel(job.id)  # queued -> cancelled
    stale = await store.update(job.model_copy(update={"status": JobStatus.RUNNING}))
    assert stale.status is JobStatus.CANCELLED

    other = make_job()
    await store.create(other)
    await store.update(other.model_copy(update={"status": JobStatus.RUNNING}))
    flagged = await store.request_cancel(other.id)
    assert flagged is not None
    assert flagged.status is JobStatus.RUNNING
    assert flagged.cancel_requested
    kept = await store.update(other.model_copy(update={"status": JobStatus.RUNNING}))
    assert kept.cancel_requested
    assert await store.request_cancel("missing") is None


async def test_transaction_retries_on_concurrent_write(
    store: RedisJobStore, redis_server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job()
    await store.create(job)
    intruder = fakeredis.FakeAsyncRedis(server=redis_server)
    real_execute = Pipeline.execute
    interfered: list[bool] = []

    async def execute(self: Pipeline, raise_on_error: bool = True) -> list[object]:
        if not interfered:  # another writer sneaks in between WATCH and EXEC once
            interfered.append(True)
            await intruder.set(
                f"job:{job.id}", job.model_copy(update={"error": "x"}).model_dump_json()
            )
        return await real_execute(self, raise_on_error)

    monkeypatch.setattr(Pipeline, "execute", execute)
    attempts = 0

    def change(stored: ResearchJob | None) -> ResearchJob:
        nonlocal attempts
        attempts += 1
        assert stored is not None
        return stored.model_copy(update={"status": JobStatus.RUNNING})

    result = await store._transact(job.id, change)
    await intruder.aclose()
    assert attempts == 2
    assert result is not None
    assert result.status is JobStatus.RUNNING
    assert result.error == "x"  # the retry saw the concurrent write


async def test_ping_reports_connection_state(
    store: RedisJobStore, redis_server: fakeredis.FakeServer
) -> None:
    assert await store.ping()
    redis_server.connected = False
    assert not await store.ping()


async def test_aclose_only_closes_owned_client(redis_server: fakeredis.FakeServer) -> None:
    shared = fakeredis.FakeAsyncRedis(server=redis_server)
    await RedisJobStore(shared, TTL).aclose()
    assert await shared.ping()  # still usable
    owned = fakeredis.FakeAsyncRedis(server=redis_server)
    await RedisJobStore(owned, TTL, owns_client=True).aclose()
    await shared.aclose()
