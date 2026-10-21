"""Claude provider built on the official ``anthropic`` SDK.

* Model, effort, token limit, timeout and retries come from :class:`Settings`.
* Adaptive thinking is the model default, so ``thinking`` is never sent (and neither are
  sampling parameters).
* Server-side refusal fallbacks (``fallbacks="default"``) are enabled through the beta
  endpoint when ``ANTHROPIC_ENABLE_FALLBACKS`` is true.
* ``research()`` uses Claude's server-side web search tool and resumes ``pause_turn``.
* Requests are streamed and collected with ``get_final_message()`` so long outputs are not
  cut off by HTTP idle timeouts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any, Final

import anthropic
from anthropic import AsyncAnthropic, omit
from anthropic.types.beta import (
    BetaMessage,
    BetaMessageParam,
    BetaToolUnionParam,
    BetaWebSearchTool20260209Param,
)

from research_assistant.config import Settings
from research_assistant.llm.base import LLMError, LLMRefusalError, LLMResult, LLMTruncatedError
from research_assistant.models import Source

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL_TYPE: Final = "web_search_20260209"
MAX_CONTINUATIONS = 5

_TRUNCATION_STOP_REASONS = frozenset({"max_tokens", "model_context_window_exceeded"})


def _merge_sources(sources: Iterable[Source]) -> list[Source]:
    """Deduplicate by exact URL, keep first-seen order, fill in missing title/snippet."""
    merged: dict[str, Source] = {}
    for source in sources:
        if not source.url:
            continue
        existing = merged.get(source.url)
        if existing is None:
            merged[source.url] = source
            continue
        if not existing.title and source.title:
            existing.title = source.title
        if not existing.snippet and source.snippet:
            existing.snippet = source.snippet
    return list(merged.values())


def extract_text(content: Sequence[Any]) -> str:
    """Concatenate the ``text`` blocks of a response (thinking and tool blocks are ignored)."""
    return "".join(block.text for block in content if getattr(block, "type", None) == "text")


def extract_sources(content: Sequence[Any]) -> list[Source]:
    """Sources from web search results and from citations on text blocks, deduplicated."""
    sources: list[Source] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "web_search_tool_result":
            results = getattr(block, "content", None)
            if not isinstance(results, list):  # a web_search_tool_result_error
                logger.info(
                    "Web search returned an error: %s", getattr(results, "error_code", results)
                )
                continue
            for item in results:
                url = getattr(item, "url", None)
                if url:
                    sources.append(Source(url=url, title=getattr(item, "title", None) or ""))
        elif block_type == "text":
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if url:
                    sources.append(
                        Source(
                            url=url,
                            title=getattr(citation, "title", None) or "",
                            snippet=getattr(citation, "cited_text", None) or "",
                        )
                    )
    return _merge_sources(sources)


def _describe_api_error(exc: anthropic.APIError) -> str:
    status = getattr(exc, "status_code", None)
    kind = type(exc).__name__
    prefix = f"{kind} (HTTP {status})" if status is not None else kind
    request_id = getattr(exc, "request_id", None)
    suffix = f" [request_id={request_id}]" if request_id else ""
    return f"Anthropic API request failed: {prefix}: {exc.message}{suffix}"


class AnthropicLLMClient:
    """:class:`~research_assistant.llm.base.LLMClient` backed by Claude.

    Args:
        settings: runtime configuration.
        client: optional pre-built ``AsyncAnthropic``-compatible client (used by tests).
    """

    name = "anthropic"

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        self.settings = settings
        if client is None:
            api_key = settings.anthropic_api_key
            client = AsyncAnthropic(
                api_key=api_key.get_secret_value() if api_key is not None else None,
                timeout=settings.anthropic_timeout_seconds,
                max_retries=settings.anthropic_max_retries,
            )
        self._client = client

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        return await self._run(system=system, prompt=prompt, max_tokens=max_tokens, tools=None)

    async def research(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        web_search: BetaWebSearchTool20260209Param = {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": self.settings.max_search_uses,
        }
        return await self._run(
            system=system, prompt=prompt, max_tokens=max_tokens, tools=[web_search]
        )

    async def aclose(self) -> None:
        await self._client.close()

    # -- internals --------------------------------------------------------------------

    async def _run(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None,
        tools: list[BetaToolUnionParam] | None,
    ) -> LLMResult:
        messages: list[BetaMessageParam] = [{"role": "user", "content": prompt}]
        content: list[Any] = []
        input_tokens = output_tokens = 0
        model = self.settings.anthropic_model
        message: BetaMessage | None = None

        for attempt in range(MAX_CONTINUATIONS + 1):
            message = await self._stream(
                system=system, messages=messages, max_tokens=max_tokens, tools=tools
            )
            content.extend(message.content)
            input_tokens += message.usage.input_tokens
            output_tokens += message.usage.output_tokens
            model = message.model or model

            if message.stop_reason == "refusal":
                details = message.stop_details
                raise LLMRefusalError(
                    getattr(details, "category", None), getattr(details, "explanation", None)
                )
            if message.stop_reason != "pause_turn":
                break
            if attempt < MAX_CONTINUATIONS:
                # Server-side tool loop paused: send the turn back so the server resumes it.
                logger.debug("pause_turn received; continuing (%d)", attempt + 1)
                messages.append({"role": "assistant", "content": message.content})

        assert message is not None
        text = extract_text(content).strip()
        if message.stop_reason in _TRUNCATION_STOP_REASONS and not text:
            raise LLMTruncatedError(
                f"The model stopped ({message.stop_reason}) before producing any text"
            )
        if message.stop_reason == "pause_turn" and not text:
            raise LLMError(
                f"The model did not finish after {MAX_CONTINUATIONS} pause_turn continuations"
            )
        if message.stop_reason in _TRUNCATION_STOP_REASONS:
            logger.warning("Model output truncated (%s); using partial text", message.stop_reason)
        return LLMResult(
            text=text,
            sources=extract_sources(content),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

    async def _stream(
        self,
        *,
        system: str,
        messages: list[BetaMessageParam],
        max_tokens: int | None,
        tools: list[BetaToolUnionParam] | None,
    ) -> BetaMessage:
        fallbacks = self.settings.anthropic_enable_fallbacks
        try:
            async with self._client.beta.messages.stream(
                model=self.settings.anthropic_model,
                max_tokens=max_tokens or self.settings.anthropic_max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": self.settings.anthropic_effort},
                tools=tools if tools is not None else omit,
                betas=[FALLBACK_BETA] if fallbacks else omit,
                fallbacks="default" if fallbacks else omit,
            ) as stream:
                return await stream.get_final_message()
        except anthropic.APIError as exc:
            raise LLMError(_describe_api_error(exc)) from exc
