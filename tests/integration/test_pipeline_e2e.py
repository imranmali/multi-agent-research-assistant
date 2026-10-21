"""Full offline run with the mock provider (spec: Offline operation, Cited report)."""

from __future__ import annotations

import socket

import pytest

from research_assistant.config import Settings
from research_assistant.llm import build_llm_client
from research_assistant.models import EventType, ProgressEvent, ResearchRequest
from research_assistant.orchestrator import Orchestrator

pytestmark = pytest.mark.integration


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted in offline mode")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.mark.usefixtures("no_network")
async def test_offline_end_to_end() -> None:
    settings = Settings(llm_provider="mock", max_subquestions=4, max_parallel_research=2)
    llm = build_llm_client(settings)
    events: list[ProgressEvent] = []

    async def on_event(event: ProgressEvent) -> None:
        events.append(event)

    try:
        report = await Orchestrator(llm, settings).run(
            ResearchRequest(question="What are the benefits of urban green roofs?"), on_event
        )
    finally:
        await llm.aclose()

    md = report.markdown
    assert report.title
    assert report.summary
    assert md.startswith(f"# {report.title}")
    for heading in ("## Executive summary", "## Findings", "## Sources"):
        assert heading in md
    assert len(report.findings) == 4
    assert all(f.ok for f in report.findings)

    # Every sub-question cites the shared overview source; it is listed exactly once.
    urls = [s.url for s in report.sources]
    assert len(urls) == len(set(urls)) == 5
    assert md.count("https://example.org/research-overview") == 1
    sources_section = md.split("## Sources", 1)[1]
    for number, url in enumerate(urls, start=1):
        assert f"{number}. " in sources_section
        assert url in sources_section

    assert (events[0].type, events[0].stage) == (EventType.STAGE_STARTED, "planning")
    assert (events[-1].type, events[-1].stage) == (EventType.STAGE_COMPLETED, "writing")
