from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator

import pytest

from research_assistant import __version__
from research_assistant.api.app import create_app
from research_assistant.api.security import is_valid_api_key
from research_assistant.jobs.service import build_job_service
from research_assistant.jobs.store import InMemoryJobStore
from research_assistant.logging import configure_logging
from tests.conftest import ApiHarness, FakeRunner, api_harness, asgi_client, make_settings

QUESTION = {"question": "What is the state of solid-state batteries?"}


# --- authentication -----------------------------------------------------------------


@pytest.fixture
async def secured() -> AsyncIterator[ApiHarness]:
    async with api_harness(make_settings(api_keys=["key-one", "key-two"])) as harness:
        yield harness


async def test_missing_key_is_401(secured: ApiHarness) -> None:
    response = await secured.client.post("/v1/research", json=QUESTION)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "APIKey"
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Missing or invalid API key"}
    }
    assert await secured.service.list() == []


async def test_invalid_key_is_401(secured: ApiHarness) -> None:
    for path in ("/v1/research", "/v1/research/abc", "/v1/research/abc/events"):
        response = await secured.client.get(path, headers={"X-API-Key": "nope"})
        assert response.status_code == 401, path


async def test_any_configured_key_is_accepted(secured: ApiHarness) -> None:
    for key in ("key-one", "key-two"):
        response = await secured.client.post(
            "/v1/research", json=QUESTION, headers={"X-API-Key": key}
        )
        assert response.status_code == 202


async def test_ops_endpoints_stay_public(secured: ApiHarness) -> None:
    assert (await secured.client.get("/healthz")).status_code == 200
    assert (await secured.client.get("/readyz")).status_code == 200
    assert (await secured.client.get("/metrics")).status_code == 200


async def test_auth_disabled_without_keys(api: ApiHarness) -> None:
    assert (await api.client.post("/v1/research", json=QUESTION)).status_code == 202


def test_key_comparison() -> None:
    assert is_valid_api_key("a", ["b", "a"])
    assert not is_valid_api_key("a", ["b"])
    assert not is_valid_api_key(None, ["a"])
    assert not is_valid_api_key("", ["a"])


# --- health, readiness, metrics -------------------------------------------------------


async def test_healthz(api: ApiHarness) -> None:
    response = await api.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok(api: ApiHarness) -> None:
    response = await api.client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"job_store": "ok"}}


class DownStore(InMemoryJobStore):
    async def ping(self) -> bool:
        raise ConnectionError("redis down")


async def test_readyz_503_when_store_unreachable() -> None:
    settings = make_settings()
    service = build_job_service(settings, runner_factory=FakeRunner().factory)
    service.store = DownStore()
    app = create_app(settings, service=service)
    async with asgi_client(app) as client:
        response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["checks"] == {"job_store": "unreachable"}
        assert body["error"]["code"] == "not_ready"
        assert (await client.get("/healthz")).status_code == 200
    await service.aclose()


async def test_not_started_app_is_not_ready() -> None:
    app = create_app(make_settings())  # lifespan not run: no service yet
    async with asgi_client(app) as client:
        ready = await client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {"job_store": "not_started"}
        response = await client.post("/v1/research", json=QUESTION)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"


async def test_metrics_exposes_http_and_job_metrics(api: ApiHarness) -> None:
    await api.client.get("/v1/research/some-id")
    response = await api.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert 'http_requests_total{method="GET",route="/v1/research/{job_id}",status="404"}' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "research_jobs_running" in text
    assert "research_job_duration_seconds" in text
    await api.client.get("/no/such/path")
    text = (await api.client.get("/metrics")).text
    assert 'route="unmatched",status="404"' in text


# --- request ids, logging, limits, errors ----------------------------------------------


async def test_request_id_is_echoed(api: ApiHarness) -> None:
    response = await api.client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


async def test_request_id_is_generated_when_missing_or_invalid(api: ApiHarness) -> None:
    generated = (await api.client.get("/healthz")).headers["x-request-id"]
    assert len(generated) == 32
    other = (await api.client.get("/healthz")).headers["x-request-id"]
    assert other != generated
    bad = await api.client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
    assert bad.headers["x-request-id"] != "bad id\twith spaces"
    error = await api.client.get("/v1/research/missing", headers={"X-Request-ID": "err-1"})
    assert error.headers["x-request-id"] == "err-1"


async def test_logs_carry_request_id(api: ApiHarness) -> None:
    buffer = io.StringIO()
    configure_logging(make_settings(log_json=True), stream=buffer)
    try:
        await api.client.post("/v1/research", json=QUESTION, headers={"X-Request-ID": "trace-7"})
    finally:
        configure_logging(make_settings())
    records = [json.loads(line) for line in buffer.getvalue().splitlines()]
    access = [r for r in records if r["event"] == "http_request"]
    assert access[0]["request_id"] == "trace-7"
    assert access[0]["status"] == 202
    assert access[0]["route"] == "/v1/research"
    submitted = [r for r in records if r["event"] == "job_submitted"]
    assert submitted[0]["request_id"] == "trace-7"


async def test_oversized_body_is_413(api: ApiHarness) -> None:
    payload = json.dumps({"question": "x" * (70 * 1024)}).encode()
    response = await api.client.post(
        "/v1/research", content=payload, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert "x-request-id" in response.headers


async def test_oversized_chunked_body_is_413(api: ApiHarness) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b"x" * (20 * 1024)

    response = await api.client.post(
        "/v1/research", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_unhandled_error_is_500_with_error_body(api: ApiHarness) -> None:
    @api.app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    response = await api.client.get("/boom", headers={"X-Request-ID": "r-500"})
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error"}
    }
    assert response.headers["x-request-id"] == "r-500"


async def test_cors_when_configured() -> None:
    async with api_harness(make_settings(cors_origins=["https://app.example"])) as api:
        response = await api.client.options(
            "/v1/research",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://app.example"
        simple = await api.client.get("/healthz", headers={"Origin": "https://app.example"})
        assert "x-request-id" in simple.headers["access-control-expose-headers"].lower()


async def test_no_cors_by_default(api: ApiHarness) -> None:
    response = await api.client.get("/healthz", headers={"Origin": "https://app.example"})
    assert "access-control-allow-origin" not in response.headers


async def test_openapi_and_docs(api: ApiHarness) -> None:
    schema = (await api.client.get("/openapi.json")).json()
    assert schema["info"]["version"] == __version__
    assert "Research" in schema["info"]["title"]
    assert "/v1/research/{job_id}/events" in schema["paths"]
    assert (await api.client.get("/docs")).status_code == 200


async def test_lifespan_builds_and_closes_service() -> None:
    app = create_app(make_settings())
    async with app.router.lifespan_context(app):
        service = app.state.job_service
        assert service is not None
        async with asgi_client(app) as client:
            assert (await client.get("/readyz")).status_code == 200
    assert app.state.job_service is None


async def test_lifespan_keeps_injected_service() -> None:
    settings = make_settings()
    service = build_job_service(settings, runner_factory=FakeRunner().factory)
    app = create_app(settings, service=service)
    async with app.router.lifespan_context(app):
        assert app.state.job_service is service
    assert app.state.job_service is service
    await service.aclose()


async def test_asgi_lifespan_passes_through_middleware() -> None:
    """Drive the raw ASGI lifespan protocol, as uvicorn does, through the middleware stack."""
    app = create_app(make_settings())
    incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return incoming.pop(0)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send)
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


def test_metrics_survive_re_registration() -> None:
    from prometheus_client import CollectorRegistry, Counter, Gauge

    from research_assistant.metrics import _get_or_create

    registry = CollectorRegistry()
    first = _get_or_create(Counter, "demo_events", "Demo.", ("kind",), registry=registry)
    again = _get_or_create(Counter, "demo_events", "Demo.", ("kind",), registry=registry)
    assert again is first
    gauge = _get_or_create(Gauge, "demo_gauge", "Demo.", registry=registry)
    assert _get_or_create(Gauge, "demo_gauge", "Demo.", registry=registry) is gauge
    with pytest.raises(ValueError, match="Duplicated"):
        _get_or_create(Gauge, "demo_events", "Clash with a counter.", registry=registry)
