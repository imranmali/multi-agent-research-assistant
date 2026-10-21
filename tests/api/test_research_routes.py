from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from research_assistant.models import JobStatus, ResearchRequest
from tests.conftest import ApiHarness, FakeRunner, api_harness, make_settings, wait_for

QUESTION = "What is the state of solid-state batteries?"


async def submit(client: httpx.AsyncClient, question: str = QUESTION) -> dict[str, Any]:
    response = await client.post("/v1/research", json={"question": question})
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


async def wait_job(
    client: httpx.AsyncClient, job_id: str, *statuses: str, within: float = 3.0
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    async def reached() -> bool:
        nonlocal result
        result = (await client.get(f"/v1/research/{job_id}")).json()
        return result["status"] in statuses

    await wait_for(reached, within)
    return result


async def test_submit_returns_202_with_links(api: ApiHarness) -> None:
    response = await api.client.post(
        "/v1/research", json={"question": QUESTION, "options": {"max_subquestions": 3}}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert set(body) == {"id", "status", "created_at", "links"}
    assert body["links"] == {
        "self": f"/v1/research/{body['id']}",
        "events": f"/v1/research/{body['id']}/events",
    }
    assert response.headers["location"] == f"/v1/research/{body['id']}"


@pytest.mark.parametrize(
    "payload",
    [
        {"question": "ab"},
        {"question": "x" * 2001},
        {},
        {"question": QUESTION, "options": {"max_subquestions": 0}},
        {"question": QUESTION, "options": {"max_subquestions": 11}},
        {"question": QUESTION, "options": {"max_revisions": 6}},
    ],
)
async def test_invalid_submission_is_422_and_creates_nothing(
    api: ApiHarness, payload: dict[str, Any]
) -> None:
    response = await api.client.post("/v1/research", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"]
    assert await api.service.list() == []


async def test_malformed_json_is_422(api: ApiHarness) -> None:
    response = await api.client.post(
        "/v1/research", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_completed_job_has_report(api: ApiHarness) -> None:
    job_id = (await submit(api.client))["id"]
    body = await wait_job(api.client, job_id, "completed")
    assert body["id"] == job_id
    assert body["question"] == QUESTION
    assert body["started_at"] is not None
    assert body["finished_at"] is not None
    assert body["error"] is None
    assert body["report"]["markdown"].startswith("# ")
    assert body["report"]["sources"][0]["url"] == "https://example.org/a"
    assert body["options"] == {
        "max_subquestions": None,
        "max_parallel_research": None,
        "max_revisions": None,
    }


async def test_failed_job_exposes_error() -> None:
    async with api_harness(runner=FakeRunner(error=RuntimeError("No evidence"))) as api:
        job_id = (await submit(api.client))["id"]
        body = await wait_job(api.client, job_id, "failed")
        assert body["error"] == "No evidence"
        assert body["report"] is None


async def test_unknown_job_is_404(api: ApiHarness) -> None:
    response = await api.client.get("/v1/research/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Research job does-not-exist not found"}
    }


async def test_list_newest_first_with_paging(api: ApiHarness) -> None:
    ids = [(await submit(api.client, f"Question number {i}"))["id"] for i in range(3)]
    response = await api.client.get("/v1/research", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert [item["id"] for item in body["items"]] == [ids[2], ids[1]]
    assert "report" not in body["items"][0]
    page2 = (await api.client.get("/v1/research", params={"limit": 2, "offset": 2})).json()
    assert [item["id"] for item in page2["items"]] == [ids[0]]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
async def test_list_bounds_are_validated(api: ApiHarness, params: dict[str, int]) -> None:
    response = await api.client.get("/v1/research", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_list_limit_follows_settings() -> None:
    async with api_harness(make_settings(max_list_limit=5)) as api:
        assert (await api.client.get("/v1/research?limit=5")).status_code == 200
        response = await api.client.get("/v1/research?limit=6")
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["loc"] == ["query", "limit"]


async def test_cancel_queued_job() -> None:
    gate = asyncio.Event()
    async with api_harness(make_settings(worker_concurrency=1), FakeRunner(gate=gate)) as api:
        blocker = (await submit(api.client, "First question here"))["id"]
        await asyncio.wait_for(api.runner.started.wait(), 1)
        queued = (await submit(api.client, "Second question here"))["id"]

        response = await api.client.delete(f"/v1/research/{queued}")
        assert response.status_code == 202
        assert response.json()["status"] == "cancelled"
        assert response.json()["cancel_requested"] is True

        gate.set()
        await wait_job(api.client, blocker, "completed")
        await asyncio.sleep(0.02)
        body = (await api.client.get(f"/v1/research/{queued}")).json()
        assert body["status"] == "cancelled"
        assert api.runner.questions == ["First question here"]
        events = await api.service.events(queued)
        assert [e.type.value for e in events] == ["job_queued", "job_cancelled"]


async def test_cancel_running_job() -> None:
    gate = asyncio.Event()
    async with api_harness(runner=FakeRunner(gate=gate)) as api:
        job_id = (await submit(api.client))["id"]
        await asyncio.wait_for(api.runner.started.wait(), 1)
        response = await api.client.delete(f"/v1/research/{job_id}")
        assert response.status_code == 202
        assert response.json()["status"] == "running"
        assert response.json()["cancel_requested"] is True
        gate.set()
        body = await wait_job(api.client, job_id, "cancelled")
        assert body["report"] is None


async def test_cancel_finished_job_is_409(api: ApiHarness) -> None:
    job_id = (await submit(api.client))["id"]
    await wait_job(api.client, job_id, "completed")
    response = await api.client.delete(f"/v1/research/{job_id}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "completed" in response.json()["error"]["message"]


async def test_cancel_unknown_job_is_404(api: ApiHarness) -> None:
    response = await api.client.delete("/v1/research/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_method_not_allowed_uses_error_body(api: ApiHarness) -> None:
    response = await api.client.put("/v1/research")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_service_rejects_ambiguous_wiring() -> None:
    from research_assistant.jobs.service import JobService
    from research_assistant.jobs.store import InMemoryJobStore

    with pytest.raises(ValueError, match="exactly one"):
        JobService(InMemoryJobStore())


async def test_cancel_race_with_completion_is_409(api: ApiHarness) -> None:
    """The job finishes between the service's read and its cancel request."""
    from research_assistant.jobs.service import JobAlreadyFinishedError

    job = await api.service.submit(ResearchRequest(question=QUESTION))
    await wait_job(api.client, job.id, "completed")
    store = api.service.store
    original_get = store.get

    async def stale_get(job_id: str) -> Any:
        current = await original_get(job_id)
        assert current is not None
        return current.model_copy(update={"status": JobStatus.RUNNING})

    store.get = stale_get  # type: ignore[method-assign]
    with pytest.raises(JobAlreadyFinishedError):
        await api.service.cancel(job.id)


async def test_cancel_of_job_that_vanishes_is_not_found(api: ApiHarness) -> None:
    from research_assistant.jobs.service import JobNotFoundError

    job = await api.service.submit(ResearchRequest(question=QUESTION))
    store = api.service.store

    async def vanished(job_id: str) -> None:
        return None

    store.request_cancel = vanished  # type: ignore[method-assign]
    with pytest.raises(JobNotFoundError):
        await api.service.cancel(job.id)
