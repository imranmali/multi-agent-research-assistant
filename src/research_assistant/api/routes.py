"""HTTP routes: ``/v1/research`` (jobs + SSE) and operational endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from research_assistant.api.deps import get_app_settings, get_job_service
from research_assistant.api.schemas import (
    ErrorBody,
    ErrorDetail,
    HealthResponse,
    JobList,
    JobSummary,
    JobView,
    ReadinessResponse,
    SubmitResponse,
)
from research_assistant.api.security import require_api_key
from research_assistant.config import Settings
from research_assistant.jobs.service import JobAlreadyFinishedError, JobNotFoundError, JobService
from research_assistant.models import EventType, ProgressEvent, ResearchRequest

__all__ = ["ops_router", "research_router"]

_TERMINAL_EVENTS = {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED}
#: Polls to keep waiting for the terminal event after the job itself is terminal.
_TERMINAL_EVENT_GRACE_POLLS = 3

ServiceDep = Annotated[JobService, Depends(get_job_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]

_errors: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorBody, "description": "Missing or invalid API key"},
}
research_router = APIRouter(
    prefix="/v1/research",
    tags=["research"],
    dependencies=[Depends(require_api_key)],
    responses=_errors,
)
ops_router = APIRouter(tags=["operations"])


class NotFoundError(Exception):
    """Raised by routes for unknown job ids; rendered as a 404 error body."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Research job {job_id} not found")


@research_router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SubmitResponse,
    responses={413: {"model": ErrorBody}, 422: {"description": "Validation error"}},
)
async def submit_research(
    body: ResearchRequest, service: ServiceDep, response: Response
) -> SubmitResponse:
    job = await service.submit(body)
    response.headers["Location"] = f"/v1/research/{job.id}"
    return SubmitResponse.from_job(job)


@research_router.get("", response_model=JobList)
async def list_research(
    service: ServiceDep,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, description="Page size (≤ MAX_LIST_LIMIT)")] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobList:
    if limit > settings.max_list_limit:
        raise RequestValidationError(
            [
                {
                    "type": "less_than_equal",
                    "loc": ("query", "limit"),
                    "msg": f"Input should be less than or equal to {settings.max_list_limit}",
                    "input": limit,
                    "ctx": {"le": settings.max_list_limit},
                }
            ]
        )
    jobs = await service.list(limit=limit, offset=offset)
    return JobList(items=[JobSummary.from_job(j) for j in jobs], limit=limit, offset=offset)


@research_router.get("/{job_id}", response_model=JobView, responses={404: {"model": ErrorBody}})
async def get_research(job_id: str, service: ServiceDep) -> JobView:
    job = await service.get(job_id)
    if job is None:
        raise NotFoundError(job_id)
    return JobView.from_job(job)


@research_router.delete(
    "/{job_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobView,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def cancel_research(job_id: str, service: ServiceDep) -> JobView | JSONResponse:
    try:
        job = await service.cancel(job_id)
    except JobNotFoundError as exc:
        raise NotFoundError(job_id) from exc
    except JobAlreadyFinishedError as exc:
        body = ErrorBody(
            error=ErrorDetail(
                code="conflict",
                message=f"Job already finished with status {exc.job.status.value}",
            )
        )
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body.model_dump())
    return JobView.from_job(job)


def format_sse(index: int, event: ProgressEvent) -> str:
    data = json.dumps({"index": index, **event.model_dump(mode="json")}, separators=(",", ":"))
    return f"id: {index}\nevent: {event.type.value}\ndata: {data}\n\n"


def _start_index(last_event_id: str | None) -> int:
    if last_event_id and last_event_id.strip().isdigit():
        return int(last_event_id.strip()) + 1
    return 0


@research_router.get(
    "/{job_id}/events",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Progress event stream"},
        404: {"model": ErrorBody},
    },
)
async def stream_research_events(
    job_id: str,
    request: Request,
    service: ServiceDep,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Server-Sent Events: replays past events, follows new ones, ends after the job ends."""
    if await service.get(job_id) is None:
        raise NotFoundError(job_id)
    poll = settings.sse_poll_interval_seconds

    async def stream() -> AsyncIterator[str]:
        next_index = _start_index(last_event_id)
        polls_after_terminal = 0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            for event in await service.events(job_id, next_index):
                yield format_sse(next_index, event)
                next_index += 1
                if event.type in _TERMINAL_EVENTS:
                    return
            job = await service.get(job_id)
            if job is None:
                return
            if job.status.terminal:
                # Status is written just before the terminal event; give it a moment.
                polls_after_terminal += 1
                if polls_after_terminal > _TERMINAL_EVENT_GRACE_POLLS:
                    return
            await asyncio.sleep(poll)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@ops_router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@ops_router.get(
    "/readyz", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}}
)
async def readyz(request: Request) -> JSONResponse:
    service: JobService | None = getattr(request.app.state, "job_service", None)
    ok = service is not None and await service.ping()
    if ok:
        body = ReadinessResponse(status="ready", checks={"job_store": "ok"})
        return JSONResponse(body.model_dump(exclude_none=True))
    body = ReadinessResponse(
        status="unavailable",
        checks={"job_store": "unreachable" if service is not None else "not_started"},
        error=ErrorDetail(code="not_ready", message="Job store is not reachable"),
    )
    return JSONResponse(body.model_dump(exclude_none=True), status_code=503)


@ops_router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
