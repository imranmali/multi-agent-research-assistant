"""Provider-agnostic LLM interface used by every agent."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from research_assistant.models import Source


class LLMError(RuntimeError):
    """Base class for provider errors that agents/orchestrator may handle."""


class LLMRefusalError(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""

    def __init__(self, category: str | None, explanation: str | None = None) -> None:
        self.category = category
        self.explanation = explanation
        detail = f"category={category or 'unknown'}"
        if explanation:
            detail += f": {explanation}"
        super().__init__(f"Model refused the request ({detail})")


class LLMTruncatedError(LLMError):
    """The model hit ``max_tokens`` before finishing."""


class LLMResult(BaseModel):
    text: str
    sources: list[Source] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@runtime_checkable
class LLMClient(Protocol):
    """What agents need from a model provider."""

    name: str

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        """Single-turn text generation without tools."""

    async def research(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        """Generation grounded in live web search; returns the sources that were consulted."""

    async def aclose(self) -> None:
        """Release network resources."""
