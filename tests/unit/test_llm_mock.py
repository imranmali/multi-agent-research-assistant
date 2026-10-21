"""Deterministic offline provider (spec: Offline operation)."""

from __future__ import annotations

import asyncio
import json

import pytest

from research_assistant.agents.prompts import (
    ANALYST_SYSTEM,
    CRITIC_SYSTEM,
    PLANNER_SYSTEM,
    RESEARCHER_SYSTEM,
    ROLE_PLANNER,
    ROLE_RESEARCHER,
    WRITER_SYSTEM,
    planner_prompt,
    researcher_prompt,
)
from research_assistant.llm.base import LLMClient, LLMError, LLMRefusalError
from research_assistant.llm.mock import SHARED_SOURCE_URL, MockLLMClient, slugify


def test_is_an_llm_client() -> None:
    client = MockLLMClient()
    assert isinstance(client, LLMClient)
    assert client.name == "mock"


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (2, 2), (3, 3), (4, 4), (10, 5)])
async def test_planner_respects_at_most(limit: int, expected: int) -> None:
    client = MockLLMClient()
    result = await client.complete(
        system=PLANNER_SYSTEM, prompt=planner_prompt("How do vaccines work?", limit)
    )
    plan = json.loads(result.text)
    assert len(plan) == expected
    assert all("vaccines work" in item["question"] for item in plan)


async def test_planner_output_override_and_determinism() -> None:
    client = MockLLMClient(planner_output="garbage")
    result = await client.complete(system=PLANNER_SYSTEM, prompt=planner_prompt("Q?", 3))
    assert result.text == "garbage"
    a = await MockLLMClient().complete(system=PLANNER_SYSTEM, prompt=planner_prompt("Q?", 3))
    b = await MockLLMClient().complete(system=PLANNER_SYSTEM, prompt=planner_prompt("Q?", 3))
    assert a.text == b.text


async def test_research_returns_slug_source_and_shared_source() -> None:
    client = MockLLMClient()
    r1 = await client.research(system=RESEARCHER_SYSTEM, prompt=researcher_prompt("Q", "Alpha?"))
    r2 = await client.research(system=RESEARCHER_SYSTEM, prompt=researcher_prompt("Q", "Beta?"))
    assert [s.url for s in r1.sources] == ["https://example.org/alpha", SHARED_SOURCE_URL]
    assert [s.url for s in r2.sources] == ["https://example.org/beta", SHARED_SOURCE_URL]
    assert "Alpha?" in r1.text
    assert r1.model == "mock-model"


async def test_injected_failure_and_refusal() -> None:
    client = MockLLMClient(fail_on={"boom"}, refuse_on={"forbidden"})
    with pytest.raises(LLMError, match="boom"):
        await client.research(system=RESEARCHER_SYSTEM, prompt="boom")
    with pytest.raises(LLMRefusalError) as info:
        await client.research(system=RESEARCHER_SYSTEM, prompt="forbidden topic")
    assert info.value.category == "general_harms"
    assert client.active_research == 0


@pytest.mark.parametrize("approves", [True, False])
async def test_critic_verdict_configurable(approves: bool) -> None:
    client = MockLLMClient(critic_approves=approves)
    verdict = json.loads((await client.complete(system=CRITIC_SYSTEM, prompt="x")).text)
    assert verdict["approved"] is approves
    assert bool(verdict["issues"]) is not approves


async def test_analyst_writer_and_unknown_roles() -> None:
    client = MockLLMClient()
    analysis = await client.complete(system=ANALYST_SYSTEM, prompt="<question>Q</question>")
    assert analysis.text.startswith("## Analysis")
    revised = await client.complete(system=ANALYST_SYSTEM, prompt="<critique>x</critique>")
    assert revised.text.startswith("## Revised analysis")
    report = await client.complete(system=WRITER_SYSTEM, prompt="<question>Why?</question>")
    assert report.text.startswith("# Research report: Why")
    assert "## Executive summary" in report.text
    other = await client.complete(system="plain", prompt="hello")
    assert "hello" in other.text


async def test_records_calls_and_tracks_concurrency() -> None:
    client = MockLLMClient(delay=0.02)
    await asyncio.gather(
        *(client.research(system=RESEARCHER_SYSTEM, prompt=f"q{i}") for i in range(3))
    )
    await client.complete(system=PLANNER_SYSTEM, prompt="<question>Q</question>")
    assert client.peak_research_concurrency == 3
    assert len(client.calls_for(ROLE_RESEARCHER)) == 3
    assert client.calls_for(ROLE_PLANNER)[0].method == "complete"
    await client.aclose()
    assert client.closed


def test_slugify() -> None:
    assert slugify("What is X? (2026)") == "what-is-x-2026"
    assert slugify("???") == "topic"
