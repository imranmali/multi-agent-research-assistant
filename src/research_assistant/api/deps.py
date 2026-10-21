"""FastAPI dependencies resolving per-app state."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from research_assistant.config import Settings
from research_assistant.jobs.service import JobService

__all__ = ["get_app_settings", "get_job_service"]


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_job_service(request: Request) -> JobService:
    service: JobService | None = getattr(request.app.state, "job_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service is starting up"
        )
    return service
