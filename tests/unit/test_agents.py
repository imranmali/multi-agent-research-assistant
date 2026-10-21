"""Individual agents and report rendering (spec: decomposition, cited report, refusals)."""

from __future__ import annotations

import json

import pytest

from research_assistant.agents import Analyst, Critic, Planner, Researcher, Writer
from research_assistant.agents.critic import PARSE_FAILURE_ISSUE, parse_critique
from research_assistant.agents.planner import parse_plan
from research_assistant.agents.prompts import (
    ROLE_ANALYST,
    ROLE_WRITER,
    format_sources,
    writer_prompt,
)
from research_assistant.agents.report import (
    collect_sources,
    dedupe_sources,
    extract_section,
    extract_title,
    first_paragraph,
    normalize_url,
    render_gaps,
    render_sources,
    strip_sections,
)
from research_assistant.llm.base import LLMError, LLMResult
from research_assistant.llm.mock import SHARED_SOURCE_URL, MockLLMClient
from research_assistant.models import Critique, Finding, Source, SubQuestion


class FixedLLM:
    """Returns the same text for every call."""

    name = "fixed"

    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.prompts: list[str] = []

    async def _answer(self, prompt: str) -> LLMResult:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return LLMResult(text=self.text)

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        return await self._answer(prompt)

    async def research(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        return await self._answer(prompt)

    async def aclose(self) -> None:
        return None


def finding(sid: int, *urls: str, error: str | None = None) -> Finding:
    return Finding(
        subquestion_id=sid,
        question=f"Sub-question {sid}?",
        summary="" if error else f"Summary {sid}",
        sources=[Source(url=u, title=f"T {u}") for u in urls],
        error=error,
    )


# -- planner -------------------------------------------------------------------------


async def test_planner_bounded_plan() -> None:
    """Scenario: Plan is bounded (max_subquestions=3)."""
    plan = await Planner(MockLLMClient()).plan("How do vaccines work?", 3)
    assert 1 <= len(plan) <= 3
    assert [sq.id for sq in plan] == [1, 2, 3]


async def test_planner_caps_model_output() -> None:
    items = [{"question": f"Q{i}?"} for i in range(8)]
    plan = await Planner(FixedLLM(json.dumps(items))).plan("Q", 3)
    assert [sq.question for sq in plan] == ["Q0?", "Q1?", "Q2?"]


@pytest.mark.parametrize("output", ["no json at all", "[]", '{"other": 1}', "[1, 2, null]"])
async def test_planner_falls_back_to_original_question(output: str) -> None:
    """Scenario: Planner output is unusable."""
    plan = await Planner(FixedLLM(output)).plan("What is dark matter?", 4)
    assert len(plan) == 1
    assert plan[0].id == 1
    assert plan[0].question == "What is dark matter?"


async def test_planner_propagates_provider_errors() -> None:
    with pytest.raises(LLMError):
        await Planner(FixedLLM(error=LLMError("down"))).plan("Q", 3)


def test_parse_plan_accepts_strings_wrapped_objects_and_dedupes() -> None:
    items = '["  A  one? ", "a ONE?", {"question": "B?", "rationale": 3}, ""]'
    text = f'```json\n{{"subquestions": {items}}}\n```'
    plan = parse_plan(text, 5)
    assert [(sq.question, sq.rationale) for sq in plan] == [("A one?", ""), ("B?", "")]


# -- researcher ----------------------------------------------------------------------


async def test_researcher_success() -> None:
    sq = SubQuestion(id=2, question="What is X?")
    result = await Researcher(MockLLMClient()).research("Overall", sq)
    assert result.ok
    assert result.subquestion_id == 2
    assert SHARED_SOURCE_URL in [s.url for s in result.sources]


async def test_researcher_records_refusal() -> None:
    """Scenario: Researcher refused."""
    sq = SubQuestion(id=1, question="forbidden topic")
    result = await Researcher(MockLLMClient(refuse_on={"forbidden"})).research("Q", sq)
    assert not result.ok
    assert "refused" in (result.error or "")
    assert "general_harms" in (result.error or "")


async def test_researcher_records_error_and_empty_answer() -> None:
    sq = SubQuestion(id=1, question="boom")
    failed = await Researcher(MockLLMClient(fail_on={"boom"})).research("Q", sq)
    assert failed.error is not None
    assert failed.error.startswith("Research failed")
    empty = await Researcher(FixedLLM("   ")).research("Q", sq)
    assert empty.error is not None
    assert "empty" in empty.error


# -- analyst / critic ----------------------------------------------------------------


async def test_analyst_analyze_and_revise() -> None:
    llm = MockLLMClient()
    analyst = Analyst(llm)
    findings = [finding(1, "https://a"), finding(2, error="x")]
    draft = await analyst.analyze("Q", findings)
    assert draft.startswith("## Analysis")
    critique = Critique(approved=False, issues=["weak"], suggestions=["cite"])
    revised = await analyst.revise("Q", findings, draft, critique)
    assert revised.startswith("## Revised analysis")
    prompt = llm.calls_for(ROLE_ANALYST)[1].prompt
    assert "weak" in prompt
    assert "<draft>" in prompt
    assert "Research failed for this sub-question: x" in prompt


async def test_analyst_empty_output_is_error() -> None:
    with pytest.raises(LLMError, match="empty analysis"):
        await Analyst(FixedLLM("")).analyze("Q", [finding(1)])


@pytest.mark.parametrize("approves", [True, False])
async def test_critic_review(approves: bool) -> None:
    critique = await Critic(MockLLMClient(critic_approves=approves)).review(
        "Q", [finding(1, "https://a")], "draft"
    )
    assert critique.approved is approves


@pytest.mark.parametrize("text", ["not json", '{"issues": []}', '{"approved": "yes"}'])
def test_critic_unparseable_approves(text: str) -> None:
    critique = parse_critique(text)
    assert critique.approved is True
    assert critique.issues == [PARSE_FAILURE_ISSUE]


def test_critic_parses_lists_leniently() -> None:
    critique = parse_critique('{"approved": false, "issues": "one", "suggestions": [1, " s "]}')
    assert critique == Critique(approved=False, issues=["one"], suggestions=["s"])


# -- writer and report helpers --------------------------------------------------------


async def test_writer_report_structure_and_gaps() -> None:
    llm = MockLLMClient()
    findings = [
        finding(1, "https://a.example", "https://shared.example"),
        finding(2, "https://shared.example/", "https://b.example"),
        finding(3, error="Model refused the request (category=cyber)"),
    ]
    plan = [SubQuestion(id=f.subquestion_id, question=f.question) for f in findings]
    critiques = [Critique(approved=False, issues=["Needs more data"])]
    report = await Writer(llm).write("Why?", plan, findings, "analysis", critiques, 1)
    assert report.title == "Research report: Why"
    assert report.summary.startswith("The available evidence")
    assert [s.url for s in report.sources] == [
        "https://a.example",
        "https://shared.example",
        "https://b.example",
    ]
    md = report.markdown
    assert md.startswith("# Research report: Why")
    assert md.index("## Executive summary") < md.index("## Findings") < md.index("## Sources")
    assert "## Research gaps" in md
    assert "category=cyber" in md
    assert md.count("(<https://shared.example") == 1
    assert report.revisions == 1
    assert report.critiques == critiques
    prompt = llm.calls_for(ROLE_WRITER)[0].prompt
    assert "Needs more data" in prompt
    assert "<unanswered_subquestions>" in prompt


async def test_writer_adds_title_and_strips_model_sources() -> None:
    text = "Plain intro paragraph.\n\n## Findings\n\nStuff [1]\n\n## References\n\n1. fake"
    report = await Writer(FixedLLM(text)).write("My question?", [], [finding(1, "https://a")], "x")
    assert report.title == "My question?"
    assert report.summary == "Plain intro paragraph."
    assert "fake" not in report.markdown
    assert report.markdown.count("## Sources") == 1


async def test_writer_empty_output_is_error() -> None:
    with pytest.raises(LLMError, match="empty report"):
        await Writer(FixedLLM("## Sources\n\n1. x")).write("Q", [], [finding(1)], "x")


def test_dedupe_sources_merges_same_url() -> None:
    """Scenario: Duplicate sources merged."""
    sources = [
        Source(url="https://Example.org/a/", title=""),
        Source(url="https://example.org/a#frag", title="A", snippet="s"),
        Source(url="https://example.org/b", title="B"),
        Source(url="  ", title="blank"),
    ]
    merged = dedupe_sources(sources)
    assert [s.url for s in merged] == ["https://Example.org/a/", "https://example.org/b"]
    assert (merged[0].title, merged[0].snippet) == ("A", "s")
    assert sources[0].title == ""  # inputs are not mutated


def test_collect_sources_skips_failed_findings() -> None:
    findings = [
        finding(1, "https://a"),
        finding(2, "https://b", error="x"),
        finding(3, "https://a"),
    ]
    assert [s.url for s in collect_sources(findings)] == ["https://a"]


def test_render_sources_numbered() -> None:
    md = render_sources([Source(url="https://a", title="A [draft]"), Source(url="https://b")])
    assert md == "## Sources\n\n1. [A \\[draft\\]](<https://a>)\n2. <https://b>"
    assert "No sources" in render_sources([])


def test_render_gaps() -> None:
    assert render_gaps([finding(1)]) == ""
    gaps = render_gaps([finding(1), finding(2, error="boom")])
    assert "Sub-question 2?" in gaps
    assert "boom" in gaps


def test_markdown_helpers() -> None:
    md = (
        "Intro\n\n# Title #\n\n## Executive Summary\n\nSum.\n\n### Detail\n\nMore.\n\n## Other\n\nX"
    )
    assert extract_title(md) == "Title"
    assert extract_title("## only h2") is None
    assert extract_section(md, ["executive summary"]) == "Sum.\n\n### Detail\n\nMore."
    assert extract_section(md, ["missing"]) is None
    assert extract_section("## Summary\n\n", ["summary"]) is None
    assert "Sum." not in strip_sections(md, ["Executive summary"])
    assert "## Other" in strip_sections(md, ["Executive summary"])
    assert first_paragraph("# T\n\n## S\n\nBody here.") == "Body here."
    assert first_paragraph("# T") == ""
    assert normalize_url("HTTPS://A.org/x/#f") == "https://a.org/x"


def test_prompt_helpers() -> None:
    assert format_sources([]) == "(no sources)"
    prompt = writer_prompt("Q", [finding(1, "https://a")], [Source(url="https://a")], "an")
    assert "[1] https://a - https://a" in prompt
    assert "<unanswered_subquestions>" not in prompt
