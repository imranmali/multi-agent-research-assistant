"""HTTP response schemas (request bodies reuse :class:`ResearchRequest`)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from research_assistant.models import JobStatus, ResearchJob, ResearchOptions, ResearchReport

__all__ = [
    "ErrorBody",
    "ErrorDetail",
    "HealthResponse",
    "JobList",
    "JobSummary",
    "JobView",
    "ReadinessResponse",
    "SubmitResponse",
]


def job_links(job_id: str) -> dict[str, str]:
    base = f"/v1/research/{job_id}"
    return {"self": base, "events": f"{base}/events"}


class SubmitResponse(BaseModel):
    id: str
    status: JobStatus
    created_at: datetime
    links: dict[str, str]

    @classmethod
    def from_job(cls, job: ResearchJob) -> SubmitResponse:
        return cls(id=job.id, status=job.status, created_at=job.created_at, links=job_links(job.id))


class JobSummary(BaseModel):
    id: str
    status: JobStatus
    question: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    cancel_requested: bool = False
    links: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_job(cls, job: ResearchJob) -> JobSummary:
        return cls(
            id=job.id,
            status=job.status,
            question=job.request.question,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            error=job.error,
            cancel_requested=job.cancel_requested,
            links=job_links(job.id),
        )


class JobView(JobSummary):
    options: ResearchOptions
    report: ResearchReport | None = None

    @classmethod
    def from_job(cls, job: ResearchJob) -> JobView:
        summary = JobSummary.from_job(job)
        return cls(**summary.model_dump(), options=job.request.options, report=job.report)


class JobList(BaseModel):
    items: list[JobSummary]
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[dict[str, object]] | None = None


class ErrorBody(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, str]
    error: ErrorDetail | None = None
