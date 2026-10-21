"""Researcher: gathers web evidence for one sub-question."""

from __future__ import annotations

import logging

from research_assistant.agents.base import BaseAgent
from research_assistant.agents.prompts import RESEARCHER_SYSTEM, researcher_prompt
from research_assistant.agents.report import dedupe_sources
from research_assistant.llm.base import LLMError, LLMRefusalError
from research_assistant.models import Finding, SubQuestion

logger = logging.getLogger(__name__)


class Researcher(BaseAgent):
    name = "researcher"

    async def research(self, question: str, subquestion: SubQuestion) -> Finding:
        """Research one sub-question. Provider failures are recorded on the Finding."""
        finding = Finding(subquestion_id=subquestion.id, question=subquestion.question)
        try:
            result = await self.llm.research(
                system=RESEARCHER_SYSTEM,
                prompt=researcher_prompt(question, subquestion.question),
            )
        except LLMRefusalError as exc:
            logger.warning("Research refused for sub-question %s: %s", subquestion.id, exc)
            finding.error = str(exc)
            return finding
        except LLMError as exc:
            logger.warning("Research failed for sub-question %s: %s", subquestion.id, exc)
            finding.error = f"Research failed: {exc}"
            return finding
        summary = result.text.strip()
        if not summary:
            finding.error = "Research failed: the model returned an empty answer"
            return finding
        finding.summary = summary
        finding.sources = dedupe_sources(result.sources)
        return finding
