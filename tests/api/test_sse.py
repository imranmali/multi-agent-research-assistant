from __future__ import annotations

import asyncio
import json
from typing import Any

from research_assistant.api.routes import format_sse
from research_assistant.models import EventType, JobStatus, ProgressEvent
from tests.conftest import ApiHarness, FakeRunner, api_harness, make_job, wait_for_status


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            fields[key] = value
        if "data" in fields:
            events.append(
                {
                    "id": int(fields["id"]),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return events


async def test_stream_replays_and_follows_until_completion() -> None:
    async with api_harness(runner=FakeRunner(delay=0.01)) as api:
        job_id = (
            await api.client.post("/v1/research", json={"question": "Why is the sky blue?"})
        ).json()["id"]
        response = await api.client.get(f"/v1/research/{job_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        events = parse_sse(response.text)
        assert [e["id"] for e in events] == list(range(len(events)))
        types = [e["event"] for e in events]
        assert types[0] == "job_queued"
        assert types[1] == "job_started"
        assert types[-1] == "job_completed"
        assert types.count("stage_started") == 3
        assert events[2]["data"]["stage"] == "planning"
        assert events[2]["data"]["index"] == 2
        assert response.text.startswith("retry: ")


async def test_last_event_id_resumes_stream(api: ApiHarness) -> None:
    job_id = (await api.client.post("/v1/research", json={"question": "Why is water wet?"})).json()[
        "id"
    ]
    await wait_for_status(api.service.get, job_id, JobStatus.COMPLETED)
    full = parse_sse((await api.client.get(f"/v1/research/{job_id}/events")).text)
    resumed = parse_sse(
        (await api.client.get(f"/v1/research/{job_id}/events", headers={"Last-Event-ID": "2"})).text
    )
    assert resumed == full[3:]
    garbage = parse_sse(
        (await api.client.get(f"/v1/research/{job_id}/events", headers={"Last-Event-ID": "x"})).text
    )
    assert garbage == full


async def test_stream_for_cancelled_job_ends(api: ApiHarness) -> None:
    gate = asyncio.Event()
    api.runner.gate = gate
    job_id = (await api.client.post("/v1/research", json={"question": "Will it stop?"})).json()[
        "id"
    ]
    await asyncio.wait_for(api.runner.started.wait(), 1)
    await api.client.delete(f"/v1/research/{job_id}")
    gate.set()
    events = parse_sse((await api.client.get(f"/v1/research/{job_id}/events")).text)
    assert events[-1]["event"] == "job_cancelled"


async def test_stream_unknown_job_is_404(api: ApiHarness) -> None:
    response = await api.client.get("/v1/research/missing/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_stream_closes_for_terminal_job_without_terminal_event(api: ApiHarness) -> None:
    job = make_job(status=JobStatus.COMPLETED)
    await api.service.store.create(job)
    await api.service.store.append_event(
        job.id, ProgressEvent(type=EventType.STAGE_STARTED, stage="planning")
    )
    response = await asyncio.wait_for(api.client.get(f"/v1/research/{job.id}/events"), 2)
    assert [e["event"] for e in parse_sse(response.text)] == ["stage_started"]


async def test_stream_stops_when_job_disappears(api: ApiHarness) -> None:
    job = make_job(status=JobStatus.RUNNING)
    await api.service.store.create(job)
    store = api.service.store
    original_get = store.get
    calls = 0

    async def vanishing_get(job_id: str) -> Any:
        nonlocal calls
        calls += 1
        return await original_get(job_id) if calls == 1 else None

    store.get = vanishing_get  # type: ignore[method-assign]
    response = await asyncio.wait_for(api.client.get(f"/v1/research/{job.id}/events"), 2)
    assert response.status_code == 200
    assert parse_sse(response.text) == []


async def test_stream_stops_when_client_disconnects(api: ApiHarness) -> None:
    job = make_job(status=JobStatus.RUNNING)  # never finishes
    await api.service.store.create(job)
    messages: list[dict[str, Any]] = []
    receive_calls = 0
    loop = asyncio.get_running_loop()
    connected_at = loop.time()

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        if loop.time() - connected_at > 0.03:  # client has gone away
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()  # nothing to receive yet (cancellable)
        raise AssertionError("unreachable")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/v1/research/{job.id}/events",
        "raw_path": f"/v1/research/{job.id}/events".encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    await asyncio.wait_for(api.app(scope, receive, send), 2)
    assert messages[0]["status"] == 200


def test_format_sse_shape() -> None:
    event = ProgressEvent(type=EventType.JOB_STARTED, stage="job", message="go")
    text = format_sse(7, event)
    assert text.startswith("id: 7\nevent: job_started\ndata: {")
    assert text.endswith("\n\n")
    assert json.loads(text.split("data: ", 1)[1])["message"] == "go"
