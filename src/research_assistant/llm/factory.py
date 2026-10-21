"""Build the configured LLM provider."""

from __future__ import annotations

from research_assistant.config import Settings
from research_assistant.llm.base import LLMClient


def build_llm_client(settings: Settings) -> LLMClient:
    """Return the provider selected by ``settings.llm_provider`` (``mock`` or ``anthropic``)."""
    provider = settings.llm_provider
    if provider == "mock":
        from research_assistant.llm.mock import MockLLMClient

        return MockLLMClient()
    if provider == "anthropic":
        from research_assistant.llm.anthropic_client import AnthropicLLMClient

        return AnthropicLLMClient(settings)
    raise ValueError(f"Unknown LLM provider: {provider!r}")
