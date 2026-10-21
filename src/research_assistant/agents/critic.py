"""Critic: reviews a draft analysis and decides whether it needs another revision."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from research_assistant.agents.base import BaseAgent
from research_assistant.agents.parsing import extract_json_object
from research_assistant.agents.prompts import CRITIC_SYSTEM, critic_prompt
from research_assistant.agents.report import collect_sources
from research_assistant.models import Critique, Finding

logger = logging.getLogger(__name__)

PARSE_FAILURE_ISSUE = "The critic's response could not be parsed; the draft was accepted as is."


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def parse_critique(text: str) -> Critique:
    """Parse the critic's JSON verdict.

    Unparseable output approves the draft (with an issue noting the parse failure) so the
    revision loop always terminates.
    """
    data = extract_json_object(text)
    if data is None or not isinstance(data.get("approved"), bool):
        logger.warning("Critic output could not be parsed; approving the draft")
        return Critique(approved=True, issues=[PARSE_FAILURE_ISSUE])
    return Critique(
        approved=data["approved"],
        issues=_strings(data.get("issues")),
        suggestions=_strings(data.get("suggestions")),
    )


class Critic(BaseAgent):
    name = "critic"

    async def review(self, question: str, findings: Sequence[Finding], draft: str) -> Critique:
        result = await self.llm.complete(
            system=CRITIC_SYSTEM,
            prompt=critic_prompt(question, findings, collect_sources(findings), draft),
        )
        return parse_critique(result.text)
