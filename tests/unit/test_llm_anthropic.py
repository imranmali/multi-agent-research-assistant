"""Claude provider, exercised against a fake SDK client (no network)."""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from research_assistant.config import Settings
from research_assistant.llm.anthropic_client import (
    FALLBACK_BETA,
    MAX_CONTINUATIONS,
    AnthropicLLMClient,
    extract_sources,
    extract_text,
)
from research_assistant.llm.base import LLMError, LLMRefusalError, LLMTruncatedError


def text_block(text: str, citations: Iterable[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text, citations=list(citations or []) or None)


def citation(url: str, title: str = "", cited: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        type="web_search_result_location", url=url, title=title, cited_text=cited
    )


def search_result(*items: tuple[str, str]) -> SimpleNamespace:
    results = [SimpleNamespace(type="web_search_result", url=u, title=t) for u, t in items]
    return SimpleNamespace(type="web_search_tool_result", tool_use_id="srvtoolu_1", content=results)


def search_error() -> SimpleNamespace:
    error = SimpleNamespace(type="web_search_tool_result_error", error_code="max_uses_exceeded")
    return SimpleNamespace(type="web_search_tool_result", tool_use_id="srvtoolu_2", content=error)


def message(
    *content: Any,
    stop_reason: str = "end_turn",
    stop_details: Any = None,
    model: str = "claude-opus-5-5",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class FakeStream:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> FakeStream:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        return self.outcome


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        # Snapshot messages: the client appends to the same list on continuation.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.outcomes.pop(0))


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.beta = SimpleNamespace(messages=FakeMessages(list(outcomes)))
        self.closed = False

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.beta.messages.calls  # type: ignore[no-any-return]

    async def close(self) -> None:
        self.closed = True


def make(*outcomes: Any, **settings: Any) -> tuple[AnthropicLLMClient, FakeClient]:
    fake = FakeClient(*outcomes)
    client = AnthropicLLMClient(Settings(**settings), client=fake)  # type: ignore[arg-type]
    return client, fake


async def test_complete_extracts_text_and_usage() -> None:
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    client, fake = make(message(thinking, text_block("Hello "), text_block("world")))
    result = await client.complete(system="sys", prompt="hi")
    assert result.text == "Hello world"
    assert result.sources == []
    assert (result.input_tokens, result.output_tokens, result.model) == (10, 5, "claude-opus-5-5")
    call = fake.calls[0]
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["model"] == "claude-opus-5-5"
    assert call["max_tokens"] == 16000
    assert call["tools"] is anthropic.omit


async def test_request_params_effort_and_no_thinking_or_sampling() -> None:
    client, fake = make(message(text_block("ok")), anthropic_effort="high")
    await client.complete(system="s", prompt="p", max_tokens=2048)
    call = fake.calls[0]
    assert call["output_config"] == {"effort": "high"}
    assert call["max_tokens"] == 2048
    for forbidden in ("thinking", "temperature", "top_p", "top_k"):
        assert forbidden not in call


async def test_fallbacks_enabled_by_default() -> None:
    client, fake = make(message(text_block("ok")))
    await client.complete(system="s", prompt="p")
    assert fake.calls[0]["betas"] == [FALLBACK_BETA]
    assert fake.calls[0]["fallbacks"] == "default"


async def test_fallbacks_disabled() -> None:
    client, fake = make(message(text_block("ok")), anthropic_enable_fallbacks=False)
    await client.complete(system="s", prompt="p")
    assert fake.calls[0]["betas"] is anthropic.omit
    assert fake.calls[0]["fallbacks"] is anthropic.omit


async def test_research_sends_web_search_and_collects_sources() -> None:
    response = message(
        search_result(("https://a.example/1", "A"), ("https://b.example/2", "B")),
        search_error(),
        text_block(
            "Fact one.",
            [citation("https://a.example/1", "A", "quoted"), citation("https://c.example", "C")],
        ),
    )
    client, fake = make(response, max_search_uses=3)
    result = await client.research(system="s", prompt="p")
    assert fake.calls[0]["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
    ]
    assert [s.url for s in result.sources] == [
        "https://a.example/1",
        "https://b.example/2",
        "https://c.example",
    ]
    # Duplicate URL merged: the later citation fills in the missing snippet.
    assert result.sources[0].snippet == "quoted"
    assert result.text == "Fact one."


def test_extract_helpers_ignore_unknown_blocks() -> None:
    blocks = [
        SimpleNamespace(type="server_tool_use", name="web_search"),
        text_block("x", [SimpleNamespace(type="char_location", cited_text="y")]),
        search_result(("", "no url")),
    ]
    assert extract_text(blocks) == "x"
    assert extract_sources(blocks) == []


async def test_pause_turn_is_continued() -> None:
    first = message(
        SimpleNamespace(type="server_tool_use", id="srvtoolu_1", name="web_search"),
        search_result(("https://a.example", "A")),
        stop_reason="pause_turn",
    )
    second = message(text_block("Done."), stop_reason="end_turn")
    client, fake = make(first, second)
    result = await client.research(system="s", prompt="p")
    assert result.text == "Done."
    assert [s.url for s in result.sources] == ["https://a.example"]
    assert (result.input_tokens, result.output_tokens) == (20, 10)
    assert len(fake.calls) == 2
    continued = fake.calls[1]["messages"]
    assert continued[0] == {"role": "user", "content": "p"}
    assert continued[1] == {"role": "assistant", "content": first.content}
    assert len(continued) == 2  # no extra "continue" user message


async def test_pause_turn_continuations_are_capped() -> None:
    paused = [message(stop_reason="pause_turn") for _ in range(MAX_CONTINUATIONS + 1)]
    client, fake = make(*paused)
    with pytest.raises(LLMError, match="pause_turn"):
        await client.research(system="s", prompt="p")
    assert len(fake.calls) == MAX_CONTINUATIONS + 1


async def test_pause_turn_cap_returns_partial_text() -> None:
    paused = [message(text_block("partial"), stop_reason="pause_turn")] * (MAX_CONTINUATIONS + 1)
    client, _ = make(*paused)
    result = await client.research(system="s", prompt="p")
    assert result.text.startswith("partial")


async def test_refusal_raises_with_category() -> None:
    details = SimpleNamespace(type="refusal", category="cyber", explanation="Not allowed")
    client, _ = make(message(stop_reason="refusal", stop_details=details))
    with pytest.raises(LLMRefusalError) as info:
        await client.research(system="s", prompt="p")
    assert info.value.category == "cyber"
    assert info.value.explanation == "Not allowed"
    assert "refused" in str(info.value)


async def test_refusal_without_details() -> None:
    client, _ = make(message(stop_reason="refusal", stop_details=None))
    with pytest.raises(LLMRefusalError) as info:
        await client.complete(system="s", prompt="p")
    assert info.value.category is None


async def test_max_tokens_without_text_raises() -> None:
    client, _ = make(message(stop_reason="max_tokens"))
    with pytest.raises(LLMTruncatedError):
        await client.complete(system="s", prompt="p")


async def test_max_tokens_with_text_returns_partial() -> None:
    client, _ = make(message(text_block("partial answer"), stop_reason="max_tokens"))
    result = await client.complete(system="s", prompt="p")
    assert result.text == "partial answer"


async def test_api_errors_are_wrapped() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(429, request=request)
    error = anthropic.RateLimitError("rate limited", response=response, body=None)
    client, _ = make(error)
    with pytest.raises(LLMError, match=r"RateLimitError \(HTTP 429\): rate limited") as info:
        await client.complete(system="s", prompt="p")
    assert info.value.__cause__ is error


async def test_connection_errors_are_wrapped() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client, _ = make(anthropic.APIConnectionError(request=request))
    with pytest.raises(LLMError, match="APIConnectionError"):
        await client.complete(system="s", prompt="p")


async def test_aclose_closes_sdk_client() -> None:
    client, fake = make()
    await client.aclose()
    assert fake.closed


async def test_builds_real_sdk_client_from_settings() -> None:
    settings = Settings(anthropic_api_key=SecretStr("sk-x"), anthropic_max_retries=0)
    client = AnthropicLLMClient(settings)
    assert isinstance(client._client, anthropic.AsyncAnthropic)
    assert client._client.api_key == "sk-x"
    await client.aclose()
