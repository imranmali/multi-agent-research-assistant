# Design

## Context

Greenfield. See proposal.md for motivation. The pipeline must run inside an API process for
development and inside horizontally scaled workers in production, so it cannot hold global
state and must be fully `async`.

## Goals / Non-Goals

**Goals:**
- Each agent is a small class with one job and one prompt, unit-testable with the mock provider.
- The orchestrator owns control flow (fan-out, limits, retries, revision loop) in plain code,
  so behaviour is deterministic and testable; the model only fills in content.
- Provider-agnostic agent code: agents depend on an `LLMClient` protocol, not on the SDK.

**Non-Goals:**
- A free-form autonomous agent loop where the model decides the control flow.
- Long-term memory across runs.

## Decisions

1. **Code-controlled workflow, not a model-driven agent loop.** The stages are known in
   advance, so a fixed pipeline is cheaper, more predictable and easier to observe than an
   open-ended tool loop. Alternative: one Claude agent with sub-agent tools — rejected for
   lower observability and harder testing.
2. **Claude server-side web search for evidence.** The Researcher calls Claude with the
   `web_search_20260209` server tool; sources come from `web_search_tool_result` blocks and
   citations. This avoids a second search vendor and API key. Alternative: Tavily/SerpAPI —
   can be added later behind the same `LLMClient.research()` method.
3. **`LLMClient` protocol with two methods**: `complete(system, prompt)` → text and
   `research(system, prompt)` → text + sources. The mock implements both deterministically.
4. **Structured planner/critic output via JSON.** Planner and critic are asked for JSON;
   parsing is tolerant (extracts the first JSON object/array) with a safe fallback, so a
   malformed answer degrades instead of crashing the run.
5. **Model defaults**: `claude-opus-5-5`, adaptive thinking (default), `output_config.effort`
   configurable (default `medium`), server-side refusal fallbacks (`fallbacks: "default"`,
   beta `server-side-fallback-2026-07-01`), SDK retries for 429/5xx. Refusals
   (`stop_reason == "refusal"`) raise `LLMRefusalError`.
6. **Concurrency** via `asyncio.Semaphore(max_parallel_research)`; per-sub-question failures
   are captured, not propagated, until all have failed.
7. **Progress events** are delivered through an async callback `on_event(ProgressEvent)` so
   the API layer can persist/stream them without the orchestrator knowing about storage.

## Risks / Trade-offs

- [Web search latency/cost grows with sub-questions] → bounded `max_subquestions`,
  `max_parallel_research`, and `max_search_uses` per sub-question.
- [Model returns non-JSON for planner/critic] → tolerant parser + fallback behaviour (spec'd).
- [Refusals on sensitive topics] → server-side fallback first, then surfaced as a stage error.
- [Mock diverges from real model behaviour] → mock only guarantees structure; a manual
  smoke test with a real key is documented in the README.
