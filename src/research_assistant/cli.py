"""``research-assistant`` command line: run research locally, serve the API, run a worker."""

from __future__ import annotations

import asyncio
import sys
from enum import StrEnum
from typing import Annotated

import typer

from research_assistant import __version__
from research_assistant.config import Settings, get_settings
from research_assistant.logging import configure_logging
from research_assistant.models import (
    ProgressEvent,
    ResearchOptions,
    ResearchReport,
    ResearchRequest,
)

app = typer.Typer(
    name="research-assistant",
    help="Multi-agent research assistant powered by Claude.",
    no_args_is_help=True,
    add_completion=False,
)

APP_FACTORY = "research_assistant.api.app:create_app"


class Provider(StrEnum):
    mock = "mock"
    anthropic = "anthropic"


def _progress(event: ProgressEvent) -> None:
    typer.echo(f"[{event.type.value}] {event.stage}: {event.message}", err=True)


async def _research(settings: Settings, request: ResearchRequest) -> ResearchReport:
    from research_assistant.llm.factory import build_llm_client
    from research_assistant.orchestrator import Orchestrator

    llm = build_llm_client(settings)
    try:

        async def on_event(event: ProgressEvent) -> None:
            _progress(event)

        return await Orchestrator(llm, settings).run(request, on_event)
    finally:
        await llm.aclose()


@app.command()
def run(
    question: Annotated[str, typer.Argument(help="The research question.")],
    provider: Annotated[
        Provider | None, typer.Option(help="LLM provider (defaults to LLM_PROVIDER).")
    ] = None,
    max_subquestions: Annotated[
        int | None, typer.Option(min=1, max=10, help="Maximum sub-questions to research.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full report as JSON instead of Markdown.")
    ] = False,
) -> None:
    """Research QUESTION now and print the report (progress goes to stderr)."""
    settings = get_settings()
    if provider is not None:
        settings = settings.model_copy(update={"llm_provider": provider.value})
    configure_logging(settings.model_copy(update={"log_json": False, "log_level": "WARNING"}))
    try:
        request = ResearchRequest(
            question=question, options=ResearchOptions(max_subquestions=max_subquestions)
        )
    except ValueError as exc:
        typer.echo(f"Invalid request: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    try:
        report = asyncio.run(_research(settings, request))
    except Exception as exc:
        typer.echo(f"Research failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(report.markdown)


@app.command()
def serve(
    host: Annotated[str, typer.Option(envvar="HOST", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="PORT", help="Bind port.")] = 8000,
    workers: Annotated[int, typer.Option(min=1, help="Uvicorn worker processes.")] = 1,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes (dev only).")] = False,
) -> None:
    """Serve the HTTP API with uvicorn."""
    import uvicorn

    configure_logging(get_settings())
    uvicorn.run(
        APP_FACTORY,
        factory=True,
        host=host,
        port=port,
        workers=workers,
        reload=reload,
        log_config=None,
        proxy_headers=True,
    )


@app.command()
def worker() -> None:
    """Run a queue worker (requires JOB_BACKEND=redis)."""
    from research_assistant.jobs.worker import main

    main()


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
