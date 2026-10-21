"""Planner: decomposes a research question into focused sub-questions."""

from __future__ import annotations

import logging
from typing import Any

from research_assistant.agents.base import BaseAgent
from research_assistant.agents.parsing import extract_json
from research_assistant.agents.prompts import PLANNER_SYSTEM, planner_prompt
from research_assistant.models import SubQuestion

logger = logging.getLogger(__name__)

_LIST_KEYS = ("subquestions", "sub_questions", "questions")


def _items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in _LIST_KEYS:
            if isinstance(value.get(key), list):
                return list(value[key])
    return []


def parse_plan(text: str, max_subquestions: int) -> list[SubQuestion]:
    """Turn planner output into at most ``max_subquestions`` sub-questions (may be empty)."""
    plan: list[SubQuestion] = []
    seen: set[str] = set()
    for item in _items(extract_json(text)):
        if isinstance(item, str):
            question, rationale = item, ""
        elif isinstance(item, dict) and isinstance(item.get("question"), str):
            question = item["question"]
            raw_rationale = item.get("rationale")
            rationale = raw_rationale if isinstance(raw_rationale, str) else ""
        else:
            continue
        question = " ".join(question.split())
        if not question or question.lower() in seen:
            continue
        seen.add(question.lower())
        plan.append(SubQuestion(id=len(plan) + 1, question=question, rationale=rationale.strip()))
        if len(plan) >= max_subquestions:
            break
    return plan


class Planner(BaseAgent):
    name = "planner"

    async def plan(self, question: str, max_subquestions: int) -> list[SubQuestion]:
        """Return 1..``max_subquestions`` sub-questions.

        Falls back to the original question when the model output is unusable. Provider
        errors (:class:`~research_assistant.llm.base.LLMError`) propagate to the caller.
        """
        limit = max(1, max_subquestions)
        result = await self.llm.complete(
            system=PLANNER_SYSTEM, prompt=planner_prompt(question, limit)
        )
        plan = parse_plan(result.text, limit)
        if not plan:
            logger.warning("Planner output unusable; researching the original question")
            return [SubQuestion(id=1, question=question, rationale="Fallback: original question")]
        return plan
