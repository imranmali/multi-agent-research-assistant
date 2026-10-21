from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import fakeredis
import pytest

from research_assistant.config import Settings
from research_assistant.jobs import worker as worker_module
from research_assistant.jobs.executor import INTERRUPTED_MESSAGE, JobExecutor
from research_assistant.jobs.queue import InMemoryJobQueue, RedisJobQueue
from research_assistant.jobs.store import InMemoryJobStore, RedisJobStore
from research_assistant.jobs.worker import Worker, default_consumer_id, run_worker
from research_assistant.models import JobStatus
from tests.conftest import FakeRunner, make_job, make_settings, wait_for, wait_for_status


async def enqueue_jobs(store: Any, queue: Any, count: int) -> list[str]:
    ids = []
    for i in range(count):
        job = make_job(f"question {i}")
        await store.create(job)
        await queue.enqueue(job.id)
        ids.append(job.id)
    return ids


def build(
    fake: FakeRunner, settings: Settings, **kwargs: Any
) -> tuple[InMemoryJobStore, InMemoryJobQueue, Worker]:
    store, queue = InMemoryJobStore(), InMemoryJobQueue()
    worker = Worker(
        settings,
        store,
        queue,
        JobExecutor(store, fake.factory),
        dequeue_timeout=0.01,
        **kwargs,
    )
    return store, queue, worker


async def test_bounded_concurrency(settings: Settings) -> None:
    fake = FakeRunner(delay=0.02)
    store, queue, worker = build(fake, settings, concurrency=2, consumer_id="w1")
    assert worker.consumer_id == "w1"
    ids = await enqueue_jobs(store, queue, 5)
    task = asyncio.create_task(worker.run())
    for job_id in ids:
        await wait_for_status(store.get, job_id, JobStatus.COMPLETED)
    await worker.stop()
    await task
    assert fake.max_active == 2
    assert fake.calls == 5
    assert worker.processed == 5


async def test_concurrency_defaults_to_settings() -> None:
    _, _, worker = build(FakeRunner(), make_settings(worker_concurrency=3))
    assert worker.concurrency == 3
    assert worker.grace == 2.0


async def test_graceful_stop_finishes_running_job(settings: Settings) -> None:
    gate = asyncio.Event()
    fake = FakeRunner(gate=gate)
    store, queue, worker = build(fake, settings, concurrency=1)
    ids = await enqueue_jobs(store, queue, 2)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(fake.started.wait(), 1)
    worker.request_stop()  # SIGTERM equivalent
    assert worker.stopping
    asyncio.get_running_loop().call_later(0.05, gate.set)
    await asyncio.wait_for(task, 2)
    first = await store.get(ids[0])
    second = await store.get(ids[1])
    assert first is not None
    assert first.status is JobStatus.COMPLETED
    assert second is not None
    assert second.status is JobStatus.QUEUED  # not picked up after stop
    assert queue.qsize() == 1


async def test_stop_cancels_jobs_exceeding_grace() -> None:
    settings = make_settings(worker_shutdown_grace_seconds=0.02)
    fake = FakeRunner(gate=asyncio.Event())  # never finishes
    store, queue, worker = build(fake, settings, concurrency=1)
    ids = await enqueue_jobs(store, queue, 1)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(fake.started.wait(), 1)
    await asyncio.wait_for(worker.stop(), 2)
    await task
    job = await store.get(ids[0])
    assert job is not None
    assert job.status is JobStatus.FAILED
    assert job.error == INTERRUPTED_MESSAGE


async def test_zero_grace_cancels_immediately() -> None:
    settings = make_settings(worker_shutdown_grace_seconds=0)
    fake = FakeRunner(gate=asyncio.Event())
    store, queue, worker = build(fake, settings, concurrency=1)
    await enqueue_jobs(store, queue, 1)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(fake.started.wait(), 1)
    await asyncio.wait_for(worker.stop(), 2)
    await task
    assert worker.active == 0


async def test_dequeue_and_ack_errors_do_not_kill_the_loop(settings: Settings) -> None:
    class FlakyQueue(InMemoryJobQueue):
        def __init__(self) -> None:
            super().__init__()
            self.dequeue_failures = 1
            self.ack_failures = 1

        async def dequeue(self, timeout: float) -> str | None:  # noqa: ASYNC109
            if self.dequeue_failures:
                self.dequeue_failures -= 1
                raise ConnectionError("redis blip")
            return await super().dequeue(timeout)

        async def ack(self, job_id: str) -> None:
            if self.ack_failures:
                self.ack_failures -= 1
                raise ConnectionError("redis blip")

    fake = FakeRunner()
    store, queue = InMemoryJobStore(), FlakyQueue()
    worker = Worker(settings, store, queue, JobExecutor(store, fake.factory), dequeue_timeout=0.01)
    ids = await enqueue_jobs(store, queue, 2)
    task = asyncio.create_task(worker.run())
    for job_id in ids:
        await wait_for_status(store.get, job_id, JobStatus.COMPLETED)
    await worker.stop()
    await task
    assert queue.dequeue_failures == 0
    assert queue.ack_failures == 0


async def test_two_workers_one_job_executed_once(
    settings: Settings, redis_server: fakeredis.FakeServer
) -> None:
    clients = [fakeredis.FakeAsyncRedis(server=redis_server) for _ in range(3)]
    store = RedisJobStore(clients[0], settings.job_ttl_seconds)
    producer = RedisJobQueue(clients[0], "q", "api")
    fake = FakeRunner(delay=0.02)
    workers = [
        Worker(
            settings,
            RedisJobStore(client, settings.job_ttl_seconds),
            RedisJobQueue(client, "q", f"w{i}"),
            JobExecutor(RedisJobStore(client, settings.job_ttl_seconds), fake.factory),
            dequeue_timeout=0.01,
        )
        for i, client in enumerate(clients[1:])
    ]
    tasks = [asyncio.create_task(w.run()) for w in workers]
    ids = await enqueue_jobs(store, producer, 1)
    await wait_for_status(store.get, ids[0], JobStatus.COMPLETED)
    for w in workers:
        await w.stop()
    await asyncio.gather(*tasks)
    assert fake.calls == 1
    assert sum(w.processed for w in workers) == 1
    for w in workers:  # acknowledged: nothing left in processing lists
        assert isinstance(w.queue, RedisJobQueue)
        assert await w.queue.in_flight() == []
    for client in clients:
        await client.aclose()


def test_default_consumer_id_is_unique() -> None:
    assert default_consumer_id() != default_consumer_id()


def test_metrics_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_METRICS_PORT", raising=False)
    assert worker_module._metrics_port() == 9100
    monkeypatch.setenv("WORKER_METRICS_PORT", "0")
    assert worker_module._metrics_port() == 0
    monkeypatch.setenv("WORKER_METRICS_PORT", "nope")
    with pytest.raises(SystemExit, match="WORKER_METRICS_PORT"):
        worker_module._metrics_port()


def test_main_requires_redis_backend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker_module, "get_settings", make_settings)
    monkeypatch.setattr(worker_module, "configure_logging", lambda settings: None)
    with pytest.raises(SystemExit) as excinfo:
        worker_module.main()
    assert excinfo.value.code == 2
    assert "JOB_BACKEND=redis" in capsys.readouterr().err


def test_main_runs_worker_with_redis_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Settings] = []

    async def fake_run_worker(settings: Settings) -> None:
        calls.append(settings)

    monkeypatch.setattr(worker_module, "get_settings", lambda: make_settings(job_backend="redis"))
    monkeypatch.setattr(worker_module, "configure_logging", lambda settings: None)
    monkeypatch.setattr(worker_module, "run_worker", fake_run_worker)
    worker_module.main()
    assert calls[0].job_backend == "redis"


async def test_run_worker_executes_jobs_and_stops_on_sigterm(
    redis: fakeredis.FakeAsyncRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(job_backend="redis", queue_name="research:queue")
    started_ports: list[int] = []
    monkeypatch.setattr(
        "prometheus_client.start_http_server", lambda port: started_ports.append(port)
    )
    task = asyncio.create_task(
        run_worker(settings, redis=redis, metrics_port=9999, dequeue_timeout=0.01)
    )
    store = RedisJobStore(redis, settings.job_ttl_seconds)
    queue = RedisJobQueue(redis, settings.queue_name, "producer")
    ids = await enqueue_jobs(store, queue, 1)
    job = await wait_for_status(store.get, ids[0], JobStatus.COMPLETED, within=10)
    assert job.report is not None
    assert job.report.sources
    os.kill(os.getpid(), signal.SIGTERM)
    worker = await asyncio.wait_for(task, 5)
    assert worker.processed == 1
    assert started_ports == [9999]
    assert await redis.ping()  # injected client is left open for its owner


async def test_run_worker_owns_client_it_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    server = fakeredis.FakeServer()
    created: list[fakeredis.FakeAsyncRedis] = []

    def from_url(url: str) -> fakeredis.FakeAsyncRedis:
        client = fakeredis.FakeAsyncRedis(server=server)
        created.append(client)
        return client

    monkeypatch.setattr(worker_module.Redis, "from_url", from_url)
    settings = make_settings(job_backend="redis")
    task = asyncio.create_task(
        run_worker(settings, metrics_port=0, install_signal_handlers=False, dequeue_timeout=0.01)
    )

    async def worker_started() -> bool:
        return bool(created)

    await wait_for(worker_started)
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(created) == 1
