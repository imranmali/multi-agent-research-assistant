"""Runs the research pipeline: plan -> parallel research -> analysis -> critique loop -> report.

Control flow lives here in plain code; the agents only produce content. Progress is reported
through an optional async ``on_event`` callback. Exceptions raised by that callback propagate
out of :meth:`Orchestrator.run` unchanged (the job layer uses this for cancellation).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from research_assistant.agents import Analyst, Critic, Planner, Researcher, Writer
from research_assistant.config import Settings
from research_assistant.llm.base import LLMClient, LLMError
from research_assistant.models import (
    Critique,
    EventType,
    Finding,
    ProgressEvent,
    ResearchReport,
    ResearchRequest,
    SubQuestion,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[ProgressEvent], Awaitable[None]]


class ResearchError(RuntimeError):
    """The run could not produce a report (e.g. no evidence could be gathered)."""


@dataclass(frozen=True)
class _Limits:
    max_subquestions: int
    max_parallel_research: int
    max_revisions: int


class Orchestrator:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings
        self.planner = Planner(llm)
        self.researcher = Researcher(llm)
        self.analyst = Analyst(llm)
        self.critic = Critic(llm)
        self.writer = Writer(llm)

    def _limits(self, request: ResearchRequest) -> _Limits:
        opts = request.options
        s = self.settings
        return _Limits(
            max_subquestions=(
                opts.max_subquestions if opts.max_subquestions is not None else s.max_subquestions
            ),
            max_parallel_research=(
                opts.max_parallel_research
                if opts.max_parallel_research is not None
                else s.max_parallel_research
            ),
            max_revisions=(
                opts.max_revisions if opts.max_revisions is not None else s.max_revisions
            ),
        )

    async def run(
        self, request: ResearchRequest, on_event: EventCallback | None = None
    ) -> ResearchReport:
        run = _Run(self, request, self._limits(request), on_event)
        return await run.execute()


class _Run:
    """State for a single execution of the pipeline."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        request: ResearchRequest,
        limits: _Limits,
        on_event: EventCallback | None,
    ) -> None:
        self.o = orchestrator
        self.question = request.question.strip()
        self.limits = limits
        self.on_event = on_event

    async def emit(self, type_: EventType, stage: str, message: str, **data: object) -> None:
        if self.on_event is not None:
            await self.on_event(ProgressEvent(type=type_, stage=stage, message=message, data=data))

    async def fail(self, stage: str, message: str, cause: BaseException | None = None) -> None:
        await self.emit(EventType.STAGE_FAILED, stage, message)
        raise ResearchError(message) from cause

    async def execute(self) -> ResearchReport:
        logger.info("Research run started: %r (limits=%s)", self.question, self.limits)
        plan = await self.plan()
        findings = await self.gather(plan)
        draft = await self.analyze(findings)
        draft, critiques, revisions = await self.review_loop(findings, draft)
        report = await self.write(plan, findings, draft, critiques, revisions)
        logger.info(
            "Research run finished: %d findings, %d sources, %d revisions",
            len(findings),
            len(report.sources),
            revisions,
        )
        return report

    # -- stages -----------------------------------------------------------------------

    async def plan(self) -> list[SubQuestion]:
        await self.emit(EventType.STAGE_STARTED, "planning", "Planning sub-questions")
        try:
            plan = await self.o.planner.plan(self.question, self.limits.max_subquestions)
        except LLMError as exc:
            await self.fail("planning", f"Planning failed: {exc}", exc)
        await self.emit(
            EventType.STAGE_COMPLETED,
            "planning",
            f"Planned {len(plan)} sub-question(s)",
            subquestions=[sq.model_dump() for sq in plan],
        )
        return plan

    async def gather(self, plan: list[SubQuestion]) -> list[Finding]:
        semaphore = asyncio.Semaphore(self.limits.max_parallel_research)
        tasks = [
            asyncio.create_task(self.research_one(semaphore, sq), name=f"research:{sq.id}")
            for sq in plan
        ]
        try:
            findings = list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if not any(finding.ok for finding in findings):
            errors = "; ".join(f"#{f.subquestion_id}: {f.error}" for f in findings)
            await self.fail(
                "research",
                f"No evidence could be gathered: research failed for every sub-question ({errors})",
            )
        return findings

    async def research_one(self, semaphore: asyncio.Semaphore, sq: SubQuestion) -> Finding:
        stage = f"research:{sq.id}"
        async with semaphore:
            await self.emit(EventType.STAGE_STARTED, stage, f"Researching: {sq.question}")
            try:
                finding = await self.o.researcher.research(self.question, sq)
            except Exception as exc:  # defensive: one sub-question must not abort the run
                logger.exception("Unexpected error researching sub-question %s", sq.id)
                finding = Finding(
                    subquestion_id=sq.id, question=sq.question, error=f"Unexpected error: {exc}"
                )
            if finding.ok:
                await self.emit(
                    EventType.STAGE_COMPLETED,
                    stage,
                    f"Found {len(finding.sources)} source(s)",
                    sources=len(finding.sources),
                )
            else:
                await self.emit(EventType.STAGE_FAILED, stage, finding.error or "Research failed")
            return finding

    async def analyze(self, findings: list[Finding]) -> str:
        await self.emit(EventType.STAGE_STARTED, "analysis", "Analysing findings")
        try:
            draft = await self.o.analyst.analyze(self.question, findings)
        except LLMError as exc:
            await self.fail("analysis", f"Analysis failed: {exc}", exc)
        await self.emit(EventType.STAGE_COMPLETED, "analysis", "Draft analysis ready")
        return draft

    async def review_loop(
        self, findings: list[Finding], draft: str
    ) -> tuple[str, list[Critique], int]:
        critiques: list[Critique] = []
        revisions = 0
        while True:
            stage = f"critique:{len(critiques) + 1}"
            await self.emit(EventType.STAGE_STARTED, stage, "Reviewing the draft")
            try:
                critique = await self.o.critic.review(self.question, findings, draft)
            except LLMError as exc:
                # The draft is still usable; stop reviewing rather than failing the run.
                logger.warning("Critique failed: %s", exc)
                await self.emit(EventType.STAGE_FAILED, stage, f"Critique failed: {exc}")
                break
            critiques.append(critique)
            await self.emit(
                EventType.STAGE_COMPLETED,
                stage,
                "Draft approved" if critique.approved else f"{len(critique.issues)} issue(s)",
                approved=critique.approved,
                issues=critique.issues,
            )
            if critique.approved or revisions >= self.limits.max_revisions:
                break
            stage = f"revision:{revisions + 1}"
            await self.emit(EventType.STAGE_STARTED, stage, "Revising the analysis")
            try:
                draft = await self.o.analyst.revise(self.question, findings, draft, critique)
            except LLMError as exc:
                logger.warning("Revision failed: %s", exc)
                await self.emit(EventType.STAGE_FAILED, stage, f"Revision failed: {exc}")
                break
            revisions += 1
            await self.emit(EventType.STAGE_COMPLETED, stage, "Revision ready")
        return draft, critiques, revisions

    async def write(
        self,
        plan: list[SubQuestion],
        findings: list[Finding],
        draft: str,
        critiques: list[Critique],
        revisions: int,
    ) -> ResearchReport:
        await self.emit(EventType.STAGE_STARTED, "writing", "Writing the report")
        try:
            report = await self.o.writer.write(
                self.question, plan, findings, draft, critiques, revisions
            )
        except LLMError as exc:
            await self.fail("writing", f"Writing failed: {exc}", exc)
        await self.emit(
            EventType.STAGE_COMPLETED,
            "writing",
            "Report ready",
            sources=len(report.sources),
        )
        return report
