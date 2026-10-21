"""Shared fixtures and fakes for the job layer, API and CLI tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import fakeredis
import httpx
import pytest
from fastapi import FastAPI

from research_assistant.api.app import create_app
from research_assistant.config import Settings
from research_assistant.jobs.executor import EventCallback
from research_assistant.jobs.service import JobService, build_job_service
from research_assistant.models import (
    EventType,
    JobStatus,
    ProgressEvent,
    ResearchJob,
    ResearchReport,
    ResearchRequest,
    Source,
)


def make_settings(**overrides: Any) -> Settings:
    """Settings isolated from ``.env``; fast polling and short grace periods for tests."""
    values: dict[str, Any] = {
        "environment": "test",
        "llm_provider": "mock",
        "log_json": False,
        "job_backend": "memory",
        "api_keys": [],
        "cors_origins": [],
        "sse_poll_interval_seconds": 0.01,
        "worker_shutdown_grace_seconds": 2.0,
        "worker_concurrency": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_report(question: str = "What is tested?") -> ResearchReport:
    source = Source(url="https://example.org/a", title="A", snippet="snippet")
    return ResearchReport(
        question=question,
        title=f"Report: {question}",
        summary="Summary.",
        markdown=f"# Report: {question}\n\nBody [1].\n\n## Sources\n1. https://example.org/a\n",
        sources=[source],
    )


def make_job(question: str = "What is tested?", **fields: Any) -> ResearchJob:
    return ResearchJob(request=ResearchRequest(question=question), **fields)


class FakeRunner:
    """Stand-in for the Orchestrator. One instance can serve many concurrent runs.

    * ``stages`` - stage names; a started/completed event pair is emitted for each.
    * ``delay`` - seconds to sleep inside every stage.
    * ``gate`` - when set, every run waits for it after its first ``stage_started`` event.
    * ``error`` - raised after the stages instead of returning a report.
    * ``swallow`` - catch exceptions raised by ``on_event`` (a misbehaving pipeline).
    """

    def __init__(
        self,
        *,
        stages: Iterable[str] = ("planning", "research:1", "writing"),
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
        error: BaseException | None = None,
        swallow: bool = False,
    ) -> None:
        self.stages = list(stages)
        self.delay = delay
        self.gate = gate
        self.error = error
        self.swallow = swallow
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.questions: list[str] = []

    async def _emit(self, on_event: EventCallback | None, event: ProgressEvent) -> None:
        if on_event is None:
            return
        try:
            await on_event(event)
        except Exception:
            if not self.swallow:
                raise

    async def run(
        self, request: ResearchRequest, on_event: EventCallback | None = None
    ) -> ResearchReport:
        self.calls += 1
        self.questions.append(request.question)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            for index, stage in enumerate(self.stages):
                await self._emit(on_event, ProgressEvent(type=EventType.STAGE_STARTED, stage=stage))
                if index == 0 and self.gate is not None:
                    await self.gate.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)
                await self._emit(
                    on_event, ProgressEvent(type=EventType.STAGE_COMPLETED, stage=stage)
                )
            if self.error is not None:
                raise self.error
            return make_report(request.question)
        finally:
            self.active -= 1

    def factory(self) -> FakeRunner:
        return self


async def wait_for(
    predicate: Callable[[], Awaitable[bool]], within: float = 3.0, interval: float = 0.005
) -> None:
    """Poll ``predicate`` until true or fail the test."""
    async with asyncio.timeout(within):
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(interval)


async def wait_for_status(
    get: Callable[[str], Awaitable[ResearchJob | None]],
    job_id: str,
    *statuses: JobStatus,
    within: float = 3.0,
) -> ResearchJob:
    async def done() -> bool:
        job = await get(job_id)
        return job is not None and job.status in statuses

    await wait_for(done, within)
    job = await get(job_id)
    assert job is not None
    return job


def asgi_client(app: FastAPI, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", **kwargs
    )


@dataclass
class ApiHarness:
    app: FastAPI
    client: httpx.AsyncClient
    service: JobService
    runner: FakeRunner
    settings: Settings


@asynccontextmanager
async def api_harness(
    settings: Settings | None = None, runner: FakeRunner | None = None, **client_kwargs: Any
) -> AsyncIterator[ApiHarness]:
    """App on the memory backend whose jobs are executed by a :class:`FakeRunner`."""
    settings = settings or make_settings()
    runner = runner or FakeRunner()
    service = build_job_service(settings, runner_factory=runner.factory)
    app = create_app(settings, service=service)
    try:
        async with asgi_client(app, **client_kwargs) as client:
            yield ApiHarness(app, client, service, runner, settings)
    finally:
        await service.aclose()


@pytest.fixture
async def api(settings: Settings, fake_runner: FakeRunner) -> AsyncIterator[ApiHarness]:
    async with api_harness(settings, fake_runner) as harness:
        yield harness


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
async def redis(redis_server: fakeredis.FakeServer) -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    client = fakeredis.FakeAsyncRedis(server=redis_server)
    yield client
    await client.aclose()
