"""Domain models shared by the agents, the orchestrator, the job layer and the API."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Source(BaseModel):
    """A web source the research relied on."""

    url: str
    title: str = ""
    snippet: str = ""


class ResearchOptions(BaseModel):
    """Per-request limits. ``None`` means "use the server default from Settings"."""

    max_subquestions: int | None = Field(default=None, ge=1, le=10)
    max_parallel_research: int | None = Field(default=None, ge=1, le=16)
    max_revisions: int | None = Field(default=None, ge=0, le=5)


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    options: ResearchOptions = Field(default_factory=ResearchOptions)


class SubQuestion(BaseModel):
    id: int
    question: str
    rationale: str = ""


class Finding(BaseModel):
    """Evidence gathered for one sub-question."""

    subquestion_id: int
    question: str
    summary: str = ""
    sources: list[Source] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Critique(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class ResearchReport(BaseModel):
    question: str
    title: str
    summary: str
    markdown: str
    subquestions: list[SubQuestion] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    critiques: list[Critique] = Field(default_factory=list)
    revisions: int = 0


class EventType(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"


class ProgressEvent(BaseModel):
    """One step of progress. ``stage`` is e.g. ``planning``, ``research:2``, ``critique:1``."""

    type: EventType
    stage: str
    message: str = ""
    timestamp: datetime = Field(default_factory=utcnow)
    data: dict[str, object] = Field(default_factory=dict)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class ResearchJob(BaseModel):
    id: str = Field(default_factory=new_id)
    request: ResearchRequest
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    report: ResearchReport | None = None
    cancel_requested: bool = False
