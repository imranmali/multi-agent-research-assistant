"""Analyst: synthesises findings into a draft analysis and revises it after critique."""

from __future__ import annotations

from collections.abc import Sequence

from research_assistant.agents.base import BaseAgent
from research_assistant.agents.prompts import ANALYST_SYSTEM, analyst_prompt, revision_prompt
from research_assistant.agents.report import collect_sources
from research_assistant.llm.base import LLMError
from research_assistant.models import Critique, Finding


class Analyst(BaseAgent):
    name = "analyst"

    async def analyze(self, question: str, findings: Sequence[Finding]) -> str:
        result = await self.llm.complete(
            system=ANALYST_SYSTEM,
            prompt=analyst_prompt(question, findings, collect_sources(findings)),
        )
        return _require_text(result.text, "analysis")

    async def revise(
        self, question: str, findings: Sequence[Finding], draft: str, critique: Critique
    ) -> str:
        result = await self.llm.complete(
            system=ANALYST_SYSTEM,
            prompt=revision_prompt(question, findings, collect_sources(findings), draft, critique),
        )
        return _require_text(result.text, "revision")


def _require_text(text: str, what: str) -> str:
    text = text.strip()
    if not text:
        raise LLMError(f"The model returned an empty {what}")
    return text
