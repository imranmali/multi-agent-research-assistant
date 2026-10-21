"""Deterministic, offline LLM provider for tests, CI and demos.

The mock never touches the network. It recognises which agent is calling from the role tag
in the system prompt (see :mod:`research_assistant.agents.prompts`) and returns output with
the structure that agent expects.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from research_assistant.agents.prompts import (
    ROLE_ANALYST,
    ROLE_CRITIC,
    ROLE_PLANNER,
    ROLE_RESEARCHER,
    ROLE_WRITER,
)
from research_assistant.llm.base import LLMError, LLMRefusalError, LLMResult
from research_assistant.models import Source

MOCK_MODEL = "mock-model"
SHARED_SOURCE_URL = "https://example.org/research-overview"

_PLAN_TEMPLATES = (
    ("What is the background and current state of {topic}?", "Establishes context."),
    ("What are the main mechanisms or drivers behind {topic}?", "Explains how it works."),
    ("What evidence supports or challenges the key claims about {topic}?", "Tests claims."),
    ("What are the practical implications and trade-offs of {topic}?", "Grounds the answer."),
    ("What open questions or future developments are expected for {topic}?", "Looks ahead."),
)
_MAX_PLAN = len(_PLAN_TEMPLATES)


@dataclass(frozen=True)
class MockCall:
    """One recorded call to the mock."""

    method: str
    role: str
    system: str
    prompt: str


def _tag(text: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return match.group(1).strip() if match else None


def slugify(text: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "topic"


def _topic(question: str) -> str:
    return question.strip().rstrip("?.! ") or "the topic"


class MockLLMClient:
    """Offline :class:`~research_assistant.llm.base.LLMClient` with canned, structured output.

    Args:
        critic_approves: verdict returned by the critic role.
        fail_on: substrings; a ``research()`` prompt containing any of them raises ``LLMError``.
        refuse_on: substrings; a ``research()`` prompt containing any of them raises
            ``LLMRefusalError`` (category ``general_harms``).
        delay: seconds to sleep in every call, to make concurrency observable in tests.
        planner_output: raw text to return for the planner role instead of the generated plan.
    """

    name = "mock"

    def __init__(
        self,
        *,
        critic_approves: bool = True,
        fail_on: Iterable[str] = (),
        refuse_on: Iterable[str] = (),
        delay: float = 0.0,
        planner_output: str | None = None,
    ) -> None:
        self.critic_approves = critic_approves
        self.fail_on = set(fail_on)
        self.refuse_on = set(refuse_on)
        self.delay = delay
        self.planner_output = planner_output
        self.calls: list[MockCall] = []
        self.closed = False
        self.active_research = 0
        self.peak_research_concurrency = 0

    # -- LLMClient --------------------------------------------------------------------

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        role = self._role(system)
        self.calls.append(MockCall("complete", role, system, prompt))
        await self._sleep()
        if role == ROLE_PLANNER:
            text = self._plan(prompt)
        elif role == ROLE_ANALYST:
            text = self._analysis(prompt)
        elif role == ROLE_CRITIC:
            text = self._critique()
        elif role == ROLE_WRITER:
            text = self._report(prompt)
        else:
            text = f"Mock response to: {prompt[:200]}"
        return self._result(text)

    async def research(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        role = self._role(system)
        self.calls.append(MockCall("research", role, system, prompt))
        self.active_research += 1
        self.peak_research_concurrency = max(self.peak_research_concurrency, self.active_research)
        try:
            await self._sleep()
            for marker in self.refuse_on:
                if marker in prompt:
                    raise LLMRefusalError("general_harms", "Mock refusal for testing")
            for marker in self.fail_on:
                if marker in prompt:
                    raise LLMError(f"Mock research failure (matched {marker!r})")
            subquestion = _tag(prompt, "subquestion") or prompt.strip()
            slug = slugify(subquestion)
            sources = [
                Source(
                    url=f"https://example.org/{slug}",
                    title=f"Evidence on {_topic(subquestion)}",
                    snippet=f"Key facts about {_topic(subquestion)}.",
                ),
                Source(
                    url=SHARED_SOURCE_URL,
                    title="Research overview",
                    snippet="A broad overview cited by several sub-questions.",
                ),
            ]
            text = (
                f"Evidence for **{subquestion}**\n\n"
                f"- A primary source describes {_topic(subquestion)} in detail.\n"
                "- A broad overview corroborates the main points."
            )
            return self._result(text, sources)
        finally:
            self.active_research -= 1

    async def aclose(self) -> None:
        self.closed = True

    # -- helpers ----------------------------------------------------------------------

    def calls_for(self, role: str) -> list[MockCall]:
        return [call for call in self.calls if call.role == role]

    @staticmethod
    def _role(system: str) -> str:
        for role in (ROLE_PLANNER, ROLE_RESEARCHER, ROLE_ANALYST, ROLE_CRITIC, ROLE_WRITER):
            if role in system:
                return role
        return "unknown"

    async def _sleep(self) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)

    @staticmethod
    def _result(text: str, sources: list[Source] | None = None) -> LLMResult:
        return LLMResult(
            text=text,
            sources=sources or [],
            input_tokens=0,
            output_tokens=len(text.split()),
            model=MOCK_MODEL,
        )

    def _plan(self, prompt: str) -> str:
        if self.planner_output is not None:
            return self.planner_output
        topic = _topic(_tag(prompt, "question") or prompt)
        match = re.search(r"at most (\d+)", prompt)
        limit = int(match.group(1)) if match else _MAX_PLAN
        count = max(1, min(_MAX_PLAN, limit))
        plan = [
            {"question": question.format(topic=topic), "rationale": rationale}
            for question, rationale in _PLAN_TEMPLATES[:count]
        ]
        return json.dumps(plan)

    @staticmethod
    def _analysis(prompt: str) -> str:
        question = _tag(prompt, "question") or "the question"
        finding_count = prompt.count("<finding ")
        revised = "<critique>" in prompt
        heading = "Revised analysis" if revised else "Analysis"
        return (
            f"## {heading}\n\n"
            f"This analysis of *{question}* draws on {finding_count} research findings [1].\n\n"
            "- The evidence agrees on the main points [1][2].\n"
            "- Some details remain uncertain and are flagged as such."
        )

    def _critique(self) -> str:
        if self.critic_approves:
            verdict = {"approved": True, "issues": [], "suggestions": []}
        else:
            verdict = {
                "approved": False,
                "issues": ["Some claims need stronger support."],
                "suggestions": ["Tie each claim to a numbered source."],
            }
        return json.dumps(verdict)

    @staticmethod
    def _report(prompt: str) -> str:
        question = _tag(prompt, "question") or "the research question"
        return (
            f"# Research report: {_topic(question)}\n\n"
            "## Executive summary\n\n"
            f"The available evidence gives a consistent answer to *{question}* [1].\n\n"
            "## Findings\n\n"
            "### Main points\n\n"
            "The sources agree on the central facts [1][2].\n\n"
            "### Open questions\n\n"
            "Some details remain uncertain."
        )
