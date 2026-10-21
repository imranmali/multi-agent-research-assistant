"""Horizontal scale-out: two API replicas and a worker sharing one (fake) Redis."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import fakeredis
import httpx
import pytest

from research_assistant.api.app import create_app
from research_assistant.config import Settings
from research_assistant.jobs.executor import JobExecutor
from research_assistant.jobs.queue import RedisJobQueue
from research_assistant.jobs.service import JobService, build_job_service
from research_assistant.jobs.store import RedisJobStore
from research_assistant.jobs.worker import Worker
from research_assistant.llm.mock import MockLLMClient
from research_assistant.orchestrator import Orchestrator
from tests.conftest import asgi_client, make_settings, wait_for

pytestmark = pytest.mark.integration


@dataclass
class Cluster:
    settings: Settings
    server: fakeredis.FakeServer
    replica_a: httpx.AsyncClient
    replica_b: httpx.AsyncClient
    services: list[JobService]
    clients: list[fakeredis.FakeAsyncRedis]

    def worker(self, consumer_id: str) -> Worker:
        client = fakeredis.FakeAsyncRedis(server=self.server)
        self.clients.append(client)
        store = RedisJobStore(client, self.settings.job_ttl_seconds)
        llm = MockLLMClient()
        return Worker(
            self.settings,
            store,
            RedisJobQueue(client, self.settings.queue_name, consumer_id),
            JobExecutor(store, lambda: Orchestrator(llm, self.settings)),
            consumer_id=consumer_id,
            dequeue_timeout=0.01,
        )


@pytest.fixture
async def cluster() -> AsyncIterator[Cluster]:
    settings = make_settings(job_backend="redis", llm_provider="mock")
    server = fakeredis.FakeServer()
    clients = [fakeredis.FakeAsyncRedis(server=server) for _ in range(2)]
    services = [build_job_service(settings, redis=c) for c in clients]
    apps = [create_app(settings, service=s) for s in services]
    async with asgi_client(apps[0]) as a, asgi_client(apps[1]) as b:
        yield Cluster(settings, server, a, b, services, clients)
    for service in services:
        await service.aclose()
    for client in clients:
        await client.aclose()


async def get_status(client: httpx.AsyncClient, job_id: str) -> str:
    status: str = (await client.get(f"/v1/research/{job_id}")).json()["status"]
    return status


async def test_submit_on_a_execute_on_worker_read_on_b(cluster: Cluster) -> None:
    submitted = await cluster.replica_a.post(
        "/v1/research", json={"question": "How do heat pumps work in cold climates?"}
    )
    assert submitted.status_code == 202
    job_id = submitted.json()["id"]

    # Replica B sees the queued job immediately.
    seen = (await cluster.replica_b.get(f"/v1/research/{job_id}")).json()
    assert seen["status"] == "queued"

    worker = cluster.worker("worker-1")
    task = asyncio.create_task(worker.run())

    async def completed() -> bool:
        return await get_status(cluster.replica_b, job_id) == "completed"

    await wait_for(completed, within=10)
    await worker.stop()
    await task

    body = (await cluster.replica_b.get(f"/v1/research/{job_id}")).json()
    assert body["report"]["sources"]
    assert body["report"]["markdown"]
    assert [i["id"] for i in (await cluster.replica_a.get("/v1/research")).json()["items"]] == [
        job_id
    ]
    events = (await cluster.replica_b.get(f"/v1/research/{job_id}/events")).text
    assert "event: job_completed" in events
    assert worker.processed == 1
    assert isinstance(worker.queue, RedisJobQueue)
    assert await worker.queue.in_flight() == []


async def test_cancel_on_b_before_pickup_is_skipped_by_worker(cluster: Cluster) -> None:
    job_id = (
        await cluster.replica_a.post("/v1/research", json={"question": "Will this be skipped?"})
    ).json()["id"]
    cancelled = await cluster.replica_b.delete(f"/v1/research/{job_id}")
    assert cancelled.status_code == 202
    assert cancelled.json()["status"] == "cancelled"

    worker = cluster.worker("worker-2")
    task = asyncio.create_task(worker.run())

    async def processed() -> bool:
        return worker.processed == 1

    await wait_for(processed, within=5)
    await worker.stop()
    await task
    body = (await cluster.replica_a.get(f"/v1/research/{job_id}")).json()
    assert body["status"] == "cancelled"
    assert body["started_at"] is None
    assert (await cluster.replica_a.delete(f"/v1/research/{job_id}")).status_code == 409


async def test_redis_service_reports_readiness(cluster: Cluster) -> None:
    assert (await cluster.replica_a.get("/readyz")).status_code == 200
    cluster.server.connected = False
    assert (await cluster.replica_b.get("/readyz")).status_code == 503
    cluster.server.connected = True


async def test_build_job_service_creates_and_closes_its_own_client() -> None:
    service = build_job_service(make_settings(job_backend="redis"))
    assert isinstance(service.store, RedisJobStore)
    assert isinstance(service.queue, RedisJobQueue)
    assert service.runner is None
    await service.aclose()  # never connected; closing must still succeed
