from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from research_assistant import __version__, cli
from research_assistant.config import Settings
from research_assistant.models import ResearchReport, ResearchRequest
from tests.conftest import make_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def test_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = make_settings()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


def test_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_run_with_mock_provider_prints_markdown() -> None:
    result = runner.invoke(
        cli.app, ["run", "What is the state of solid-state batteries?", "--provider", "mock"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.lstrip().startswith("#")
    assert "http" in result.stdout  # cited sources
    assert "[stage_started] planning" in result.stderr


def test_run_json_output_respects_max_subquestions() -> None:
    result = runner.invoke(
        cli.app,
        ["run", "How do heat pumps work?", "--max-subquestions", "2", "--json"],
    )
    assert result.exit_code == 0, result.output
    report = ResearchReport.model_validate(json.loads(result.stdout))
    assert 1 <= len(report.subquestions) <= 2
    assert report.sources


def test_run_rejects_invalid_question() -> None:
    result = runner.invoke(cli.app, ["run", "a"])
    assert result.exit_code == 2
    assert "Invalid request" in result.stderr


def test_run_reports_pipeline_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Settings, request: ResearchRequest) -> ResearchReport:
        raise RuntimeError("no evidence")

    monkeypatch.setattr(cli, "_research", boom)
    result = runner.invoke(cli.app, ["run", "What is going on?"])
    assert result.exit_code == 1
    assert "Research failed: no evidence" in result.stderr


def test_run_provider_override(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def capture(settings: Settings, request: ResearchRequest) -> ResearchReport:
        seen.append(settings.llm_provider)
        raise RuntimeError("stop")

    monkeypatch.setattr(cli, "_research", capture)
    runner.invoke(cli.app, ["run", "What is going on?", "--provider", "anthropic"])
    assert seen == ["anthropic"]


def test_serve_runs_uvicorn_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "9000"])
    assert result.exit_code == 0, result.output
    (args, kwargs) = calls[0]
    assert args == ("research_assistant.api.app:create_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000
    assert kwargs["workers"] == 1
    assert kwargs["reload"] is False


def test_serve_reads_host_and_port_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "8081")
    result = runner.invoke(cli.app, ["serve"])
    assert result.exit_code == 0, result.output
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["port"] == 8081


def test_worker_command_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr("research_assistant.jobs.worker.main", lambda: called.append(True))
    result = runner.invoke(cli.app, ["worker"])
    assert result.exit_code == 0
    assert called == [True]
