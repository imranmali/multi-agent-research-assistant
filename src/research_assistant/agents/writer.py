"""Writer: produces the final cited Markdown report."""

from __future__ import annotations

from collections.abc import Sequence

from research_assistant.agents.base import BaseAgent
from research_assistant.agents.prompts import WRITER_SYSTEM, writer_prompt
from research_assistant.agents.report import (
    collect_sources,
    extract_section,
    extract_title,
    first_paragraph,
    render_gaps,
    render_sources,
    strip_sections,
)
from research_assistant.llm.base import LLMError
from research_assistant.models import Critique, Finding, ResearchReport, SubQuestion

_SUMMARY_HEADINGS = ("executive summary", "summary")
_GENERATED_HEADINGS = ("sources", "references", "bibliography", "research gaps")


class Writer(BaseAgent):
    name = "writer"

    async def write(
        self,
        question: str,
        subquestions: Sequence[SubQuestion],
        findings: Sequence[Finding],
        analysis: str,
        critiques: Sequence[Critique] = (),
        revisions: int = 0,
    ) -> ResearchReport:
        sources = collect_sources(findings)
        last = critiques[-1] if critiques else None
        open_issues = last.issues if last is not None and not last.approved else []
        result = await self.llm.complete(
            system=WRITER_SYSTEM,
            prompt=writer_prompt(question, findings, sources, analysis, open_issues),
        )
        body = strip_sections(result.text, _GENERATED_HEADINGS)
        if not body:
            raise LLMError("The model returned an empty report")

        title = extract_title(body)
        if title is None:
            title = question.strip()
            body = f"# {title}\n\n{body}"
        summary = extract_section(body, _SUMMARY_HEADINGS) or first_paragraph(body)

        sections = [body, render_gaps(findings), render_sources(sources)]
        markdown = "\n\n".join(section for section in sections if section) + "\n"
        return ResearchReport(
            question=question,
            title=title,
            summary=summary,
            markdown=markdown,
            subquestions=list(subquestions),
            findings=list(findings),
            sources=sources,
            critiques=list(critiques),
            revisions=revisions,
        )
