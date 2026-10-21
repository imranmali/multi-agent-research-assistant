"""System prompts and prompt builders for every agent.

Each system prompt starts with a role tag (``[role:<name>]``). The tags make the prompts easy
to identify in logs and let the offline :class:`~research_assistant.llm.mock.MockLLMClient`
decide which kind of answer to produce. User-supplied text is wrapped in XML-style tags so the
model can tell instructions from data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from research_assistant.models import Critique, Finding, Source

ROLE_PLANNER = "[role:planner]"
ROLE_RESEARCHER = "[role:researcher]"
ROLE_ANALYST = "[role:analyst]"
ROLE_CRITIC = "[role:critic]"
ROLE_WRITER = "[role:writer]"

ALL_ROLES = (ROLE_PLANNER, ROLE_RESEARCHER, ROLE_ANALYST, ROLE_CRITIC, ROLE_WRITER)

PLANNER_SYSTEM = f"""{ROLE_PLANNER}
You are the planner on a research team. You break a research question into a small set of
focused sub-questions that, answered together, give a complete and balanced answer.

Good sub-questions are specific enough to research with a few web searches, do not overlap,
and together cover the context, the core evidence, competing views and practical
implications that the question calls for. Prefer fewer, sharper sub-questions over many
shallow ones.

Respond with only a JSON array, no prose, in this shape:
[{{"question": "...", "rationale": "why this sub-question matters"}}]"""

RESEARCHER_SYSTEM = f"""{ROLE_RESEARCHER}
You are a researcher on a research team. Use web search to answer one sub-question with
current, verifiable evidence.

Prefer primary and authoritative sources (official documentation, peer-reviewed work,
standards bodies, reputable news) and note publication dates when recency matters. When
sources disagree, say so and explain which is better supported. If the evidence is thin or
inconclusive, state that plainly rather than filling the gap.

Write a concise factual summary in Markdown (a few short paragraphs or bullet points) that
cites the sources you relied on. Do not add a separate reference list."""

ANALYST_SYSTEM = f"""{ROLE_ANALYST}
You are the analyst on a research team. You receive evidence that researchers gathered for
each sub-question of a research question and synthesise it into one coherent analysis.

Connect the findings, resolve or explain contradictions, distinguish well-supported
conclusions from tentative ones, and point out where evidence is missing. Refer to sources
by the bracketed numbers given in the evidence, for example [2]; never invent sources or
facts that the evidence does not support.

Write the analysis in Markdown, organised by theme rather than by sub-question. When you are
given a critique of an earlier draft, revise the draft to address every valid point and
return the complete revised analysis, not a list of changes."""

CRITIC_SYSTEM = f"""{ROLE_CRITIC}
You are the critic on a research team. You review a draft analysis against the evidence it
was built from and decide whether it is ready to become the final report.

Check that claims are supported by the cited evidence, that citations point to the right
sources, that important findings are not missing, that uncertainty is stated honestly, and
that the analysis actually answers the research question. Approve the draft when remaining
issues are minor; do not withhold approval over style.

Respond with only a JSON object, no prose, in this shape:
{{"approved": true, "issues": ["specific problem"], "suggestions": ["specific fix"]}}"""

WRITER_SYSTEM = f"""{ROLE_WRITER}
You are the writer on a research team. You turn an approved analysis into the final research
report for a reader who has not seen any of the intermediate work.

Write in clear, neutral Markdown with this structure:
# <a specific, informative title>
## Executive summary
(one or two short paragraphs that directly answer the question)
## Findings
(the substance, organised under ### subheadings)
## Limitations
(only if evidence was missing, contested or a part of the question could not be researched)

Cite sources inline with the bracketed numbers from the numbered source list, for example
[1] or [2][3]. Only use numbers from that list. Do not write a sources or references section;
it is appended automatically."""


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def planner_prompt(question: str, max_subquestions: int) -> str:
    return (
        f"<question>{question}</question>\n\n"
        f"Propose at most {max_subquestions} sub-questions for this research question."
    )


def researcher_prompt(question: str, subquestion: str) -> str:
    return (
        f"Today's date is {_today()}.\n\n"
        f"Overall research question, for context:\n<question>{question}</question>\n\n"
        f"Research this sub-question:\n<subquestion>{subquestion}</subquestion>"
    )


def format_sources(sources: Sequence[Source]) -> str:
    """Numbered source list used in analyst, critic and writer prompts."""
    if not sources:
        return "(no sources)"
    lines = []
    for number, source in enumerate(sources, start=1):
        title = source.title or source.url
        lines.append(f"[{number}] {title} - {source.url}")
    return "\n".join(lines)


def format_findings(findings: Sequence[Finding], sources: Sequence[Source]) -> str:
    """Render findings as tagged blocks whose source references use global numbers."""
    numbers = {source.url: number for number, source in enumerate(sources, start=1)}
    blocks = []
    for finding in findings:
        if finding.ok:
            refs = sorted({numbers[s.url] for s in finding.sources if s.url in numbers})
            ref_line = ", ".join(f"[{n}]" for n in refs) if refs else "none"
            body = f"{finding.summary.strip()}\n\nSources: {ref_line}"
        else:
            body = f"Research failed for this sub-question: {finding.error}"
        blocks.append(
            f'<finding id="{finding.subquestion_id}">\n'
            f"Sub-question: {finding.question}\n\n{body}\n</finding>"
        )
    return "\n\n".join(blocks)


def evidence_block(findings: Sequence[Finding], sources: Sequence[Source]) -> str:
    return (
        f"<evidence>\n{format_findings(findings, sources)}\n</evidence>\n\n"
        f"<sources>\n{format_sources(sources)}\n</sources>"
    )


def analyst_prompt(question: str, findings: Sequence[Finding], sources: Sequence[Source]) -> str:
    return (
        f"<question>{question}</question>\n\n"
        f"{evidence_block(findings, sources)}\n\n"
        "Write the analysis that answers the research question from this evidence."
    )


def format_critique(critique: Critique) -> str:
    issues = "\n".join(f"- {item}" for item in critique.issues) or "- (none listed)"
    suggestions = "\n".join(f"- {item}" for item in critique.suggestions) or "- (none listed)"
    return f"Issues:\n{issues}\n\nSuggestions:\n{suggestions}"


def revision_prompt(
    question: str,
    findings: Sequence[Finding],
    sources: Sequence[Source],
    draft: str,
    critique: Critique,
) -> str:
    return (
        f"<question>{question}</question>\n\n"
        f"{evidence_block(findings, sources)}\n\n"
        f"<draft>\n{draft}\n</draft>\n\n"
        f"<critique>\n{format_critique(critique)}\n</critique>\n\n"
        "Revise the draft to address the critique and return the full revised analysis."
    )


def critic_prompt(
    question: str, findings: Sequence[Finding], sources: Sequence[Source], draft: str
) -> str:
    return (
        f"<question>{question}</question>\n\n"
        f"{evidence_block(findings, sources)}\n\n"
        f"<draft>\n{draft}\n</draft>\n\n"
        "Review the draft analysis and respond with the JSON verdict."
    )


def writer_prompt(
    question: str,
    findings: Sequence[Finding],
    sources: Sequence[Source],
    analysis: str,
    open_issues: Sequence[str] = (),
) -> str:
    parts = [
        f"<question>{question}</question>",
        evidence_block(findings, sources),
        f"<analysis>\n{analysis}\n</analysis>",
    ]
    failed = [f for f in findings if not f.ok]
    if failed:
        gaps = "\n".join(f"- {f.question}" for f in failed)
        parts.append(f"<unanswered_subquestions>\n{gaps}\n</unanswered_subquestions>")
    if open_issues:
        issues = "\n".join(f"- {issue}" for issue in open_issues)
        parts.append(f"<unresolved_review_issues>\n{issues}\n</unresolved_review_issues>")
    parts.append(
        "Write the final report from the analysis. Mention unanswered sub-questions and "
        "unresolved review issues under Limitations."
    )
    return "\n\n".join(parts)
