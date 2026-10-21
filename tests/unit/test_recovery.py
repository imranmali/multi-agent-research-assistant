"""Crash recovery: jobs held by a worker that stopped heartbeating are re-queued and finish."""

from __future__ import annotations

import asyncio

import fakeredis

from research_assistant.jobs.executor import JobExecutor
from research_assistant.jobs.queue import InMemoryJobQueue, RecoverableJobQueue, RedisJobQueue
from research_assistant.jobs.store import RedisJobStore
from research_assistant.jobs.worker import Worker
from research_assistant.models import EventType, JobStatus, utcnow
from tests.conftest import FakeRunner, make_job, make_settings, wait_for_status

QUEUE = "test:queue"


def client(server: fakeredis.FakeServer) -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(server=server)


async def strand_job(server: fakeredis.FakeServer, consumer: str) -> tuple[RedisJobStore, str]:
    """Simulate a worker that dequeued a job, marked it running, then crashed."""
    store = RedisJobStore(client(server), ttl_seconds=3600)
    dead = RedisJobQueue(client(server), QUEUE, consumer)
    job = make_job("stranded question")
    await store.create(job)
    await dead.enqueue(job.id)
    assert await dead.dequeue(0) == job.id
    await store.update(job.model_copy(update={"status": JobStatus.RUNNING, "started_at": utcnow()}))
    return store, job.id


def test_only_redis_queue_is_recoverable() -> None:
    server = fakeredis.FakeServer()
    assert isinstance(RedisJobQueue(client(server), QUEUE, "c"), RecoverableJobQueue)
    assert not isinstance(InMemoryJobQueue(), RecoverableJobQueue)


async def test_live_consumers_are_left_alone() -> None:
    server = fakeredis.FakeServer()
    _, job_id = await strand_job(server, "alive")
    await RedisJobQueue(client(server), QUEUE, "alive").heartbeat(60)
    rescuer = RedisJobQueue(client(server), QUEUE, "rescuer")

    seen: list[str] = []

    async def on_recover(jid: str) -> None:
        seen.append(jid)

    assert await rescuer.recover_orphaned(on_recover) == []
    assert seen == []
    assert await RedisJobQueue(client(server), QUEUE, "alive").in_flight() == [job_id]


async def test_orphan_reset_before_it_is_visible_on_the_queue() -> None:
    server = fakeredis.FakeServer()
    _, job_id = await strand_job(server, "dead")
    rescuer = RedisJobQueue(client(server), QUEUE, "rescuer")
    redis = client(server)

    async def on_recover(jid: str) -> None:
        # The id must not be dequeueable while the store is being reset.
        assert await redis.llen(QUEUE) == 0
        assert await rescuer.in_flight() == [jid]

    assert await rescuer.recover_orphaned(on_recover) == [job_id]
    assert await rescuer.in_flight() == []
    assert await RedisJobQueue(client(server), QUEUE, "dead").in_flight() == []
    assert await rescuer.dequeue(0) == job_id


async def test_concurrent_recoverers_requeue_each_job_once() -> None:
    server = fakeredis.FakeServer()
    store = RedisJobStore(client(server), ttl_seconds=3600)
    dead = RedisJobQueue(client(server), QUEUE, "dead")
    ids = []
    for i in range(6):
        job = make_job(f"question {i}")
        await store.create(job)
        await dead.enqueue(job.id)
        assert await dead.dequeue(0) == job.id
        ids.append(job.id)

    async def noop(_: str) -> None:
        await asyncio.sleep(0)

    a = RedisJobQueue(client(server), QUEUE, "a")
    b = RedisJobQueue(client(server), QUEUE, "b")
    await a.heartbeat(60)
    await b.heartbeat(60)
    got_a, got_b = await asyncio.gather(a.recover_orphaned(noop), b.recover_orphaned(noop))
    assert sorted(got_a + got_b) == sorted(ids)
    assert await client(server).llen(QUEUE) == len(ids)


async def test_worker_recovers_and_completes_stranded_job() -> None:
    server = fakeredis.FakeServer()
    store, job_id = await strand_job(server, "crashed")
    settings = make_settings(worker_heartbeat_seconds=0.05)
    queue = RedisJobQueue(client(server), QUEUE, "survivor")
    worker = Worker(
        settings,
        store,
        queue,
        JobExecutor(store, FakeRunner().factory),
        consumer_id="survivor",
        dequeue_timeout=0.01,
    )
    task = asyncio.create_task(worker.run())
    job = await wait_for_status(store.get, job_id, JobStatus.COMPLETED)
    await worker.stop()
    await task

    assert job.report is not None
    events = await store.events(job_id)
    requeued = [e for e in events if e.type is EventType.JOB_QUEUED]
    assert requeued
    assert "stopped responding" in requeued[-1].message
    # Clean shutdown withdraws the heartbeat so peers do not wait for it to expire.
    assert not await client(server).exists(f"{QUEUE}:heartbeat:survivor")


async def test_recovered_finished_job_is_not_rerun() -> None:
    server = fakeredis.FakeServer()
    store, job_id = await strand_job(server, "crashed-after-finish")
    job = await store.get(job_id)
    assert job is not None
    await store.update(job.model_copy(update={"status": JobStatus.COMPLETED}))
    fake = FakeRunner()
    worker = Worker(
        make_settings(worker_heartbeat_seconds=0.05),
        store,
        RedisJobQueue(client(server), QUEUE, "w"),
        JobExecutor(store, fake.factory),
        consumer_id="w",
        dequeue_timeout=0.01,
    )
    task = asyncio.create_task(worker.run())
    async with asyncio.timeout(3):
        while worker.processed < 1:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    await worker.stop()
    await task
    assert fake.calls == 0
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is JobStatus.COMPLETED
