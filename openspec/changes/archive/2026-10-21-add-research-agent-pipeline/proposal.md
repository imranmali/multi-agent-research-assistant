# Proposal

## Why

Answering an open research question well needs several distinct skills: decomposing the
question, gathering evidence from the web, reconciling that evidence, checking it for gaps,
and writing a cited report. A single prompt does all of that poorly and cannot be inspected
step by step. A pipeline of specialised agents makes each step observable, testable and
independently tunable, and lets evidence gathering run in parallel.

## What Changes

- Introduce five cooperating agents: **Planner**, **Researcher**, **Analyst**, **Critic**, **Writer**.
- Introduce an **orchestrator** that runs them as a pipeline: plan → parallel research
  (bounded concurrency) → analysis → critique / revision loop (bounded iterations) → report.
- Introduce an LLM provider abstraction with two implementations:
  - `anthropic`: Claude via the official SDK, using Claude's server-side web search tool for
    evidence and server-side refusal fallbacks.
  - `mock`: deterministic, offline provider for tests, CI and demos.
- Emit structured progress events for every pipeline stage so callers can stream progress.
- Produce a final report in Markdown with numbered citations and a deduplicated source list.

Out of scope: the HTTP API, persistence and workers (see `add-research-service-api`), CI/CD
and deployment (see `add-delivery-pipeline`), non-Anthropic LLM providers, and document
upload / private-corpus retrieval.

## Capabilities

### New Capabilities
- `research-orchestration`: How a research question is decomposed, researched by multiple
  agents, critiqued and turned into a cited report, including limits, failure handling and
  progress reporting.

### Modified Capabilities
<!-- none -->

## Impact

- New package `src/research_assistant/` (`agents/`, `llm/`, `orchestrator.py`, `models.py`, `config.py`).
- New runtime dependency: `anthropic` (official SDK), `pydantic`, `pydantic-settings`.
- Requires `ANTHROPIC_API_KEY` only when `LLM_PROVIDER=anthropic`.
