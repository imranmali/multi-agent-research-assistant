"""Common base for all agents."""

from __future__ import annotations

from typing import ClassVar

from research_assistant.llm.base import LLMClient


class BaseAgent:
    """An agent has one job, one system prompt and a provider-agnostic LLM client."""

    name: ClassVar[str] = "agent"

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def __repr__(self) -> str:
        return f"{type(self).__name__}(llm={self.llm.name!r})"
