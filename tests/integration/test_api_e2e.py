"""End to end on the memory backend: real orchestrator + offline mock LLM provider."""

from __future__ import annotations

import pytest

from research_assistant.api.app import create_app
from tests.conftest import asgi_client, make_settings, wait_for

pytestmark = pytest.mark.integration


async def test_submit_poll_fetch_report_and_stream() -> None:
    settings = make_settings(llm_provider="mock", job_backend="memory", api_keys=["secret"])
    app = create_app(settings)
    headers = {"X-API-Key": "secret"}
    async with app.router.lifespan_context(app), asgi_client(app, headers=headers) as client:
        submitted = await client.post(
            "/v1/research",
            json={
                "question": "What is the state of solid-state batteries?",
                "options": {"max_subquestions": 3},
            },
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]

        body: dict[str, object] = {}

        async def completed() -> bool:
            nonlocal body
            body = (await client.get(f"/v1/research/{job_id}")).json()
            assert body["status"] not in {"failed", "cancelled"}, body
            return body["status"] == "completed"

        await wait_for(completed, within=10)
        report = body["report"]
        assert isinstance(report, dict)
        assert report["markdown"]
        assert report["sources"]
        assert 1 <= len(report["subquestions"]) <= 3
        urls = [s["url"] for s in report["sources"]]
        assert len(urls) == len(set(urls))  # deduplicated

        stream = await client.get(f"/v1/research/{job_id}/events")
        assert stream.status_code == 200
        names = [
            line.removeprefix("event: ")
            for line in stream.text.splitlines()
            if line.startswith("event: ")
        ]
        assert names[:2] == ["job_queued", "job_started"]
        assert names[-1] == "job_completed"
        assert "stage_started" in names
        assert "stage_completed" in names

        listed = (await client.get("/v1/research")).json()
        assert [item["id"] for item in listed["items"]] == [job_id]
