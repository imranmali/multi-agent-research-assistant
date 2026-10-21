"""Work queues handing job ids from the API to executors."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from redis.asyncio import Redis

__all__ = ["InMemoryJobQueue", "JobQueue", "RecoverableJobQueue", "RedisJobQueue"]


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, job_id: str) -> None:
        """Add a job id to the tail of the queue."""
        ...

    async def dequeue(self, timeout: float) -> str | None:  # noqa: ASYNC109
        """Take the next job id, waiting up to ``timeout`` seconds; ``None`` if none arrived."""
        ...

    async def ack(self, job_id: str) -> None:
        """Mark a dequeued job id as processed."""
        ...

    async def aclose(self) -> None:
        """Release resources."""
        ...


@runtime_checkable
class RecoverableJobQueue(JobQueue, Protocol):
    """A shared queue whose consumers can die; live consumers re-queue what dead ones held."""

    async def heartbeat(self, ttl_seconds: float) -> None:
        """Announce this consumer is alive for the next ``ttl_seconds``."""
        ...

    async def clear_heartbeat(self) -> None:
        """Withdraw the liveness announcement (clean shutdown)."""
        ...

    async def recover_orphaned(self, on_recover: Callable[[str], Awaitable[None]]) -> list[str]:
        """Re-queue ids held by consumers without a heartbeat, calling ``on_recover`` first."""
        ...


class InMemoryJobQueue:
    """Single-process FIFO queue (``asyncio.Queue``)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def enqueue(self, job_id: str) -> None:
        self._queue.put_nowait(job_id)

    async def dequeue(self, timeout: float) -> str | None:  # noqa: ASYNC109
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=max(timeout, 0.0))
        except TimeoutError:
            return None

    async def ack(self, job_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def qsize(self) -> int:
        return self._queue.qsize()


class RedisJobQueue:
    """Reliable queue on a Redis list.

    Producers ``LPUSH`` onto ``{name}``; each consumer atomically ``BLMOVE``\\ s the oldest
    id into its own processing list ``{name}:processing:{consumer_id}`` and ``LREM``\\ s it
    on :meth:`ack`. An id is therefore delivered to exactly one consumer, and ids held by a
    crashed consumer remain visible in its processing list for recovery.
    """

    #: Poll interval used when the server answers ``BLMOVE`` without blocking (fakeredis).
    POLL_INTERVAL = 0.02

    def __init__(
        self,
        redis: Redis,
        name: str,
        consumer_id: str,
        *,
        owns_client: bool = False,
    ) -> None:
        self._redis = redis
        self.name = name
        self.consumer_id = consumer_id
        self.processing_key = f"{name}:processing:{consumer_id}"
        self.heartbeat_key = self._heartbeat_key(consumer_id)
        self._owns_client = owns_client

    async def enqueue(self, job_id: str) -> None:
        await _resolve(self._redis.lpush(self.name, job_id))

    async def dequeue(self, timeout: float) -> str | None:  # noqa: ASYNC109
        deadline = time.monotonic() + max(timeout, 0.0)
        if timeout > 0:
            # redis-py annotates the timeout as int, but Redis >= 6 accepts fractional seconds.
            raw = await _resolve(
                self._redis.blmove(
                    self.name,
                    self.processing_key,
                    timeout,  # type: ignore[arg-type]
                    "RIGHT",
                    "LEFT",
                )
            )
            if raw is not None:
                return _text(raw)
        # Either a non-blocking request, or a server that did not block (e.g. fakeredis):
        # poll with LMOVE until the deadline.
        while True:
            raw = await self._redis.lmove(self.name, self.processing_key, "RIGHT", "LEFT")
            if raw is not None:
                return _text(raw)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(self.POLL_INTERVAL, remaining))

    async def ack(self, job_id: str) -> None:
        await _resolve(self._redis.lrem(self.processing_key, 1, job_id))

    async def in_flight(self) -> list[str]:
        """Ids this consumer has dequeued but not yet acknowledged."""
        return [_text(r) for r in await _resolve(self._redis.lrange(self.processing_key, 0, -1))]

    def _heartbeat_key(self, consumer_id: str) -> str:
        return f"{self.name}:heartbeat:{consumer_id}"

    async def heartbeat(self, ttl_seconds: float) -> None:
        await self._redis.set(self.heartbeat_key, "1", px=max(int(ttl_seconds * 1000), 1))

    async def clear_heartbeat(self) -> None:
        await self._redis.delete(self.heartbeat_key)

    async def recover_orphaned(self, on_recover: Callable[[str], Awaitable[None]]) -> list[str]:
        """Move ids out of processing lists whose consumer stopped heartbeating.

        Each id is first claimed atomically into this consumer's own processing list, so
        concurrent recoverers never handle the same id. ``on_recover`` then runs (the worker
        resets the job to ``queued``) *before* the id is pushed back to the head of the
        queue, so no consumer can dequeue it while the store still says ``running``. If this
        consumer dies mid-way, the id stays in its processing list and is recovered later.
        """
        prefix = f"{self.name}:processing:"
        recovered: list[str] = []
        async for raw_key in self._redis.scan_iter(match=f"{prefix}*"):
            key = _text(raw_key)
            consumer_id = key[len(prefix) :]
            if consumer_id == self.consumer_id:
                continue
            if await self._redis.exists(self._heartbeat_key(consumer_id)):
                continue
            while True:
                raw = await self._redis.lmove(key, self.processing_key, "RIGHT", "LEFT")
                if raw is None:
                    break
                job_id = _text(raw)
                await on_recover(job_id)
                async with self._redis.pipeline(transaction=True) as pipe:
                    pipe.lrem(self.processing_key, 1, job_id)
                    pipe.rpush(self.name, job_id)  # dequeue pops from the right: next up
                    await pipe.execute()
                recovered.append(job_id)
        return recovered

    async def aclose(self) -> None:
        if self._owns_client:
            await self._redis.aclose()


_T = TypeVar("_T")


async def _resolve(value: Awaitable[_T] | _T) -> _T:
    """Await redis-py results, whose stubs mix the sync and async return types."""
    if inspect.isawaitable(value):
        return await value
    return value


def _text(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw
