"""Provider selection."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from research_assistant.config import Settings
from research_assistant.llm import AnthropicLLMClient, MockLLMClient, build_llm_client


def test_builds_mock() -> None:
    client = build_llm_client(Settings(llm_provider="mock"))
    assert isinstance(client, MockLLMClient)


async def test_builds_anthropic() -> None:
    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key=SecretStr("sk-test"),
        anthropic_timeout_seconds=12.0,
        anthropic_max_retries=1,
    )
    client = build_llm_client(settings)
    assert isinstance(client, AnthropicLLMClient)
    assert client.name == "anthropic"
    sdk = client._client
    assert sdk.api_key == "sk-test"
    assert sdk.max_retries == 1
    assert sdk.timeout == 12.0
    await client.aclose()


def test_unknown_provider_rejected() -> None:
    settings = Settings.model_construct(llm_provider="other")
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_llm_client(settings)
