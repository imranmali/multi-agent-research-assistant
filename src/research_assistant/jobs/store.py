"""Job persistence: a small protocol with in-memory and Redis implementations.

Invariants every implementation keeps (so concurrent API replicas and workers cannot
corrupt a job):

* :meth:`JobStore.update` never moves a job out of a terminal status and never drops a
  cancellation request; it returns the job as actually persisted.
* :meth:`JobStore.request_cancel` atomically flags the job and, when it is still queued,
  moves it straight to ``cancelled``.
"""

from __future__ import annotations

import asyncio
import builtins
import time
from typing import Any, Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError

from research_assistant.jobs.queue import _resolve
from research_assistant.models import JobStatus, ProgressEvent, ResearchJob, utcnow

__all__ = ["InMemoryJobStore", "JobStore", "RedisJobStore"]


@runtime_checkable
class JobStore(Protocol):
    async def create(self, job: ResearchJob) -> None:
        """Persist a new job."""

    async def get(self, job_id: str) -> ResearchJob | None:
        """Return the job or ``None`` if unknown/expired."""

    async def update(self, job: ResearchJob) -> ResearchJob:
        """Persist ``job`` (see module invariants); return the stored version."""

    async def list(self, limit: int = 20, offset: int = 0) -> builtins.list[ResearchJob]:
        """Return jobs, newest first."""

    async def append_event(self, job_id: str, event: ProgressEvent) -> int:
        """Append a progress event; return its zero-based index."""

    async def events(self, job_id: str, since: int = 0) -> builtins.list[ProgressEvent]:
        """Return events with index ``>= since`` in order."""

    async def request_cancel(self, job_id: str) -> ResearchJob | None:
        """Flag the job for cancellation (queued jobs become cancelled). ``None`` if unknown."""

    async def ping(self) -> bool:
        """Whether the backing store is reachable."""

    async def aclose(self) -> None:
        """Release resources."""


def _merge(stored: ResearchJob | None, incoming: ResearchJob) -> ResearchJob:
    """Apply the update invariants: terminal states are final, cancel flags are sticky."""
    if stored is None:
        return incoming
    if stored.status.terminal:
        return stored
    if stored.cancel_requested and not incoming.cancel_requested:
        return incoming.model_copy(update={"cancel_requested": True})
    return incoming


def _cancel(job: ResearchJob) -> ResearchJob:
    if job.status.terminal:
        return job
    update: dict[str, Any] = {"cancel_requested": True}
    if job.status is JobStatus.QUEUED:
        update |= {"status": JobStatus.CANCELLED, "finished_at": utcnow()}
    return job.model_copy(update=update)


class InMemoryJobStore:
    """Single-process store for development and tests."""

    def __init__(self) -> None:
        self._jobs: dict[str, ResearchJob] = {}
        self._events: dict[str, builtins.list[ProgressEvent]] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: ResearchJob) -> None:
        async with self._lock:
            self._jobs[job.id] = job.model_copy(deep=True)
            self._events.setdefault(job.id, [])

    async def get(self, job_id: str) -> ResearchJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    async def update(self, job: ResearchJob) -> ResearchJob:
        async with self._lock:
            merged = _merge(self._jobs.get(job.id), job).model_copy(deep=True)
            self._jobs[job.id] = merged
            return merged.model_copy(deep=True)

    async def list(self, limit: int = 20, offset: int = 0) -> builtins.list[ResearchJob]:
        async with self._lock:
            ordered = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.model_copy(deep=True) for j in ordered[offset : offset + limit]]

    async def append_event(self, job_id: str, event: ProgressEvent) -> int:
        async with self._lock:
            bucket = self._events.setdefault(job_id, [])
            bucket.append(event.model_copy(deep=True))
            return len(bucket) - 1

    async def events(self, job_id: str, since: int = 0) -> builtins.list[ProgressEvent]:
        async with self._lock:
            return [e.model_copy(deep=True) for e in self._events.get(job_id, [])[since:]]

    async def request_cancel(self, job_id: str) -> ResearchJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            cancelled = _cancel(job)
            self._jobs[job_id] = cancelled
            return cancelled.model_copy(deep=True)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _text(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


class RedisJobStore:
    """Shared store for horizontally scaled API replicas and workers.

    Layout (every key carries the retention TTL, refreshed on each write):

    * ``job:{id}`` - the job as JSON
    * ``job:{id}:events`` - list of progress events as JSON
    * ``jobs:index`` - sorted set of job ids scored by creation time (epoch seconds)
    """

    INDEX_KEY = "jobs:index"

    def __init__(self, redis: Redis, ttl_seconds: int, *, owns_client: bool = False) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._owns_client = owns_client

    @staticmethod
    def job_key(job_id: str) -> str:
        return f"job:{job_id}"

    @staticmethod
    def events_key(job_id: str) -> str:
        return f"job:{job_id}:events"

    async def create(self, job: ResearchJob) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self.job_key(job.id), job.model_dump_json(), ex=self._ttl)
            pipe.zadd(self.INDEX_KEY, {job.id: job.created_at.timestamp()})
            pipe.expire(self.INDEX_KEY, self._ttl)
            await pipe.execute()

    async def get(self, job_id: str) -> ResearchJob | None:
        raw = await self._redis.get(self.job_key(job_id))
        return ResearchJob.model_validate_json(_text(raw)) if raw else None

    async def _transact(self, job_id: str, change: Any) -> ResearchJob | None:
        """Optimistic read-modify-write of ``job:{id}`` using WATCH/MULTI."""
        key = self.job_key(job_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    stored = ResearchJob.model_validate_json(_text(raw)) if raw else None
                    result: ResearchJob | None = change(stored)
                    if result is None:
                        await pipe.unwatch()  # type: ignore[no-untyped-call]
                        return None
                    pipe.multi()  # type: ignore[no-untyped-call]
                    pipe.set(key, result.model_dump_json(), ex=self._ttl)
                    pipe.expire(self.events_key(job_id), self._ttl)
                    await pipe.execute()
                    return result
                except WatchError:
                    continue

    async def update(self, job: ResearchJob) -> ResearchJob:
        result = await self._transact(job.id, lambda stored: _merge(stored, job))
        assert result is not None  # _merge never returns None
        return result

    async def list(self, limit: int = 20, offset: int = 0) -> builtins.list[ResearchJob]:
        horizon = time.time() - self._ttl
        await self._redis.zremrangebyscore(self.INDEX_KEY, "-inf", horizon)
        ids = [
            _text(i)
            for i in await self._redis.zrevrange(self.INDEX_KEY, offset, offset + limit - 1)
        ]
        if not ids:
            return []
        raws = await self._redis.mget([self.job_key(i) for i in ids])
        jobs: builtins.list[ResearchJob] = []
        missing: builtins.list[str] = []
        for job_id, raw in zip(ids, raws, strict=True):
            if raw is None:
                missing.append(job_id)
            else:
                jobs.append(ResearchJob.model_validate_json(_text(raw)))
        if missing:
            await self._redis.zrem(self.INDEX_KEY, *missing)
        return jobs

    async def append_event(self, job_id: str, event: ProgressEvent) -> int:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(self.events_key(job_id), event.model_dump_json())
            pipe.expire(self.events_key(job_id), self._ttl)
            pipe.expire(self.job_key(job_id), self._ttl)
            length, *_ = await pipe.execute()
        return int(length) - 1

    async def events(self, job_id: str, since: int = 0) -> builtins.list[ProgressEvent]:
        raws = await _resolve(self._redis.lrange(self.events_key(job_id), max(since, 0), -1))
        return [ProgressEvent.model_validate_json(_text(r)) for r in raws]

    async def request_cancel(self, job_id: str) -> ResearchJob | None:
        return await self._transact(job_id, lambda stored: _cancel(stored) if stored else None)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except (RedisError, OSError):
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._redis.aclose()
