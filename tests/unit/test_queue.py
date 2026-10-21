from __future__ import annotations

import asyncio
import time

import fakeredis

from research_assistant.jobs.queue import InMemoryJobQueue, JobQueue, RedisJobQueue


async def test_in_memory_queue_fifo_and_timeout() -> None:
    queue = InMemoryJobQueue()
    assert isinstance(queue, JobQueue)
    await queue.enqueue("a")
    await queue.enqueue("b")
    assert queue.qsize() == 2
    assert await queue.dequeue(0.1) == "a"
    assert await queue.dequeue(0.1) == "b"
    started = time.monotonic()
    assert await queue.dequeue(0.02) is None
    assert time.monotonic() - started >= 0.015
    await queue.ack("a")
    await queue.aclose()


async def test_in_memory_dequeue_waits_for_enqueue() -> None:
    queue = InMemoryJobQueue()

    async def later() -> None:
        await asyncio.sleep(0.02)
        await queue.enqueue("late")

    pushed = asyncio.create_task(later())
    assert await queue.dequeue(1.0) == "late"
    await pushed


async def test_redis_queue_fifo_processing_and_ack(redis: fakeredis.FakeAsyncRedis) -> None:
    queue = RedisJobQueue(redis, "q", "c1")
    assert isinstance(queue, JobQueue)
    assert queue.processing_key == "q:processing:c1"
    for job_id in ("a", "b", "c"):
        await queue.enqueue(job_id)
    assert await queue.dequeue(0.1) == "a"
    assert await queue.dequeue(0) == "b"  # non-blocking request
    assert await queue.in_flight() == ["b", "a"]
    await queue.ack("a")
    assert await queue.in_flight() == ["b"]
    await queue.ack("b")
    assert await queue.in_flight() == []
    assert await redis.llen("q") == 1


async def test_redis_queue_times_out(redis: fakeredis.FakeAsyncRedis) -> None:
    queue = RedisJobQueue(redis, "q", "c1")
    started = time.monotonic()
    assert await queue.dequeue(0.05) is None
    assert 0.04 <= time.monotonic() - started < 1.0
    assert await queue.dequeue(0) is None


async def test_redis_queue_picks_up_late_items(redis: fakeredis.FakeAsyncRedis) -> None:
    queue = RedisJobQueue(redis, "q", "c1")

    async def later() -> None:
        await asyncio.sleep(0.03)
        await queue.enqueue("late")

    pushed = asyncio.create_task(later())
    assert await queue.dequeue(1.0) == "late"
    await pushed


async def test_two_consumers_one_job_exactly_one_gets_it(
    redis_server: fakeredis.FakeServer,
) -> None:
    clients = [fakeredis.FakeAsyncRedis(server=redis_server) for _ in range(3)]
    producer = RedisJobQueue(clients[0], "q", "api")
    first = RedisJobQueue(clients[1], "q", "worker-1")
    second = RedisJobQueue(clients[2], "q", "worker-2")
    await producer.enqueue("job-1")
    results = await asyncio.gather(first.dequeue(0.1), second.dequeue(0.1))
    assert sorted(r for r in results if r is not None) == ["job-1"]
    assert results.count(None) == 1
    in_flight = await first.in_flight() + await second.in_flight()
    assert in_flight == ["job-1"]
    for client in clients:
        await client.aclose()


async def test_redis_queue_close_respects_ownership(redis_server: fakeredis.FakeServer) -> None:
    shared = fakeredis.FakeAsyncRedis(server=redis_server)
    await RedisJobQueue(shared, "q", "c").aclose()
    assert await shared.ping()
    owned = fakeredis.FakeAsyncRedis(server=redis_server)
    await RedisJobQueue(owned, "q", "c", owns_client=True).aclose()
    await shared.aclose()


async def test_resolve_accepts_plain_values() -> None:
    from research_assistant.jobs.queue import _resolve

    assert await _resolve(3) == 3

    async def coro() -> int:
        return 4

    assert await _resolve(coro()) == 4
