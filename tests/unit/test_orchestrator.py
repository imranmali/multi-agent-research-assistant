"""Pipeline control flow (spec: research-orchestration)."""

from __future__ import annotations

import pytest

from research_assistant.agents.prompts import ROLE_ANALYST, ROLE_CRITIC, ROLE_RESEARCHER
from research_assistant.config import Settings
from research_assistant.llm.base import LLMError, LLMResult
from research_assistant.llm.mock import MockLLMClient
from research_assistant.models import (
    EventType,
    ProgressEvent,
    ResearchOptions,
    ResearchRequest,
)
from research_assistant.orchestrator import Orchestrator, ResearchError

QUESTION = "How do heat pumps work in cold climates?"


def request(**options: int) -> ResearchRequest:
    return ResearchRequest(question=QUESTION, options=ResearchOptions(**options))


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    async def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    def stages(self, type_: EventType) -> list[str]:
        return [e.stage for e in self.events if e.type == type_]


def orchestrator(llm: MockLLMClient, **settings: object) -> Orchestrator:
    return Orchestrator(llm, Settings(**settings))  # type: ignore[arg-type]


async def test_plan_is_bounded_by_request_option() -> None:
    """Scenario: Plan is bounded."""
    report = await orchestrator(MockLLMClient(), max_subquestions=8).run(
        request(max_subquestions=3)
    )
    assert 1 <= len(report.subquestions) <= 3
    assert len(report.findings) == len(report.subquestions)


async def test_settings_defaults_apply_without_options() -> None:
    llm = MockLLMClient()
    report = await orchestrator(llm, max_subquestions=2).run(request())
    assert len(report.subquestions) == 2


async def test_concurrency_limit_respected() -> None:
    """Scenario: Concurrency limit respected (5 sub-questions, limit 2)."""
    llm = MockLLMClient(delay=0.02)
    report = await orchestrator(llm).run(request(max_subquestions=5, max_parallel_research=2))
    assert len(report.findings) == 5
    assert llm.peak_research_concurrency == 2


async def test_one_subquestion_fails() -> None:
    """Scenario: One sub-question fails."""
    llm = MockLLMClient(fail_on={"main mechanisms"})
    events = Recorder()
    report = await orchestrator(llm).run(request(max_subquestions=3), events)
    failed = [f for f in report.findings if not f.ok]
    assert len(failed) == 1
    assert "Mock research failure" in (failed[0].error or "")
    assert "## Research gaps" in report.markdown
    assert failed[0].question in report.markdown
    assert f"research:{failed[0].subquestion_id}" in events.stages(EventType.STAGE_FAILED)
    assert events.events[-1].type == EventType.STAGE_COMPLETED


async def test_all_subquestions_fail() -> None:
    """Scenario: All sub-questions fail."""
    llm = MockLLMClient(fail_on={QUESTION.rstrip("?")})
    events = Recorder()
    with pytest.raises(ResearchError, match="No evidence could be gathered"):
        await orchestrator(llm).run(request(max_subquestions=3), events)
    assert events.events[-1].type == EventType.STAGE_FAILED
    assert events.events[-1].stage == "research"
    assert not llm.calls_for(ROLE_ANALYST)


async def test_critic_approves_immediately() -> None:
    """Scenario: Critic approves immediately."""
    llm = MockLLMClient(critic_approves=True)
    events = Recorder()
    report = await orchestrator(llm).run(request(max_revisions=3), events)
    assert report.revisions == 0
    assert len(report.critiques) == 1
    assert not any(s.startswith("revision:") for s in events.stages(EventType.STAGE_STARTED))
    assert len(llm.calls_for(ROLE_ANALYST)) == 1


async def test_revision_budget_exhausted() -> None:
    """Scenario: Revision budget exhausted (never approves, max_revisions=2)."""
    llm = MockLLMClient(critic_approves=False)
    events = Recorder()
    report = await orchestrator(llm).run(request(max_revisions=2), events)
    assert report.revisions == 2
    revisions = [s for s in events.stages(EventType.STAGE_COMPLETED) if s.startswith("revision:")]
    assert revisions == ["revision:1", "revision:2"]
    assert len(report.critiques) == 3
    assert len(llm.calls_for(ROLE_CRITIC)) == 3
    # The report is written from the last revision.
    writer_prompt = llm.calls[-1].prompt
    assert "## Revised analysis" in writer_prompt
    assert "Some claims need stronger support." in writer_prompt


async def test_zero_revisions_still_critiques_once() -> None:
    report = await orchestrator(MockLLMClient(critic_approves=False)).run(request(max_revisions=0))
    assert report.revisions == 0
    assert len(report.critiques) == 1


async def test_event_order_for_full_run() -> None:
    """Scenario: Events observed for a full run."""
    events = Recorder()
    await orchestrator(MockLLMClient(critic_approves=False)).run(
        request(max_subquestions=2, max_revisions=1), events
    )
    sequence = [(e.type, e.stage) for e in events.events]
    assert sequence[0] == (EventType.STAGE_STARTED, "planning")
    assert sequence[1] == (EventType.STAGE_COMPLETED, "planning")
    assert sequence[-1] == (EventType.STAGE_COMPLETED, "writing")
    tail = [stage for _, stage in sequence[-10:]]
    assert tail == [
        "analysis", "analysis",
        "critique:1", "critique:1",
        "revision:1", "revision:1",
        "critique:2", "critique:2",
        "writing", "writing",
    ]  # fmt: skip
    for sid in (1, 2):
        stage = f"research:{sid}"
        assert sequence.index((EventType.STAGE_STARTED, stage)) < sequence.index(
            (EventType.STAGE_COMPLETED, stage)
        )
    assert all(e.message and e.timestamp for e in events.events)
    assert not any(e.type.value.startswith("job_") for e in events.events)


async def test_on_event_exception_propagates() -> None:
    class JobCancelledError(Exception):
        pass

    async def on_event(event: ProgressEvent) -> None:
        if event.stage == "analysis":
            raise JobCancelledError

    llm = MockLLMClient()
    with pytest.raises(JobCancelledError):
        await orchestrator(llm).run(request(), on_event)
    assert not llm.calls_for(ROLE_CRITIC)


async def test_on_event_exception_during_research_cancels_siblings() -> None:
    class JobCancelledError(Exception):
        pass

    async def on_event(event: ProgressEvent) -> None:
        if event.stage == "research:1" and event.type == EventType.STAGE_COMPLETED:
            raise JobCancelledError

    llm = MockLLMClient(delay=0.01)
    with pytest.raises(JobCancelledError):
        await orchestrator(llm).run(request(max_subquestions=4, max_parallel_research=1), on_event)
    assert len(llm.calls_for(ROLE_RESEARCHER)) < 4
    assert llm.active_research == 0


async def test_refusal_recorded() -> None:
    """Scenario: Researcher refused."""
    llm = MockLLMClient(refuse_on={"practical implications"})
    events = Recorder()
    report = await orchestrator(llm).run(request(max_subquestions=4), events)
    refused = [f for f in report.findings if not f.ok]
    assert len(refused) == 1
    assert "refused" in (refused[0].error or "")
    failed = [e for e in events.events if e.type == EventType.STAGE_FAILED]
    assert "refused" in failed[0].message


class FlakyLLM(MockLLMClient):
    """Mock whose complete() fails for one role."""

    def __init__(self, failing_role: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.failing_role = failing_role

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int | None = None
    ) -> LLMResult:
        if self.failing_role in system and (
            self.failing_role != ROLE_ANALYST or "<critique>" in prompt or self.fail_analysis
        ):
            raise LLMError(f"{self.failing_role} unavailable")
        return await super().complete(system=system, prompt=prompt, max_tokens=max_tokens)

    fail_analysis = False


@pytest.mark.parametrize(
    ("role", "stage"),
    [("[role:planner]", "planning"), ("[role:writer]", "writing")],
)
async def test_fatal_stage_failures(role: str, stage: str) -> None:
    events = Recorder()
    with pytest.raises(ResearchError, match="failed"):
        await orchestrator(FlakyLLM(role)).run(request(), events)
    assert (events.events[-1].type, events.events[-1].stage) == (EventType.STAGE_FAILED, stage)


async def test_analysis_failure_is_fatal() -> None:
    llm = FlakyLLM(ROLE_ANALYST)
    llm.fail_analysis = True
    with pytest.raises(ResearchError, match="Analysis failed"):
        await orchestrator(llm).run(request())


async def test_critique_failure_keeps_draft() -> None:
    events = Recorder()
    report = await orchestrator(FlakyLLM(ROLE_CRITIC)).run(request(), events)
    assert report.critiques == []
    assert "critique:1" in events.stages(EventType.STAGE_FAILED)


async def test_revision_failure_keeps_previous_draft() -> None:
    events = Recorder()
    llm = FlakyLLM(ROLE_ANALYST, critic_approves=False)
    report = await orchestrator(llm).run(request(max_revisions=2), events)
    assert report.revisions == 0
    assert "revision:1" in events.stages(EventType.STAGE_FAILED)
    assert events.events[-1].stage == "writing"


async def test_unexpected_researcher_exception_is_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = orchestrator(MockLLMClient())
    original = orch.researcher.research

    async def broken(question, subquestion):  # type: ignore[no-untyped-def]
        if subquestion.id == 1:
            raise ValueError("bug")
        return await original(question, subquestion)

    monkeypatch.setattr(orch.researcher, "research", broken)
    report = await orch.run(request(max_subquestions=3))
    assert report.findings[0].error == "Unexpected error: bug"
    assert all(f.ok for f in report.findings[1:])


async def test_run_without_callback() -> None:
    report = await orchestrator(MockLLMClient()).run(request())
    assert report.markdown
