# Tasks

## 1. Project scaffolding

- [ ] 1.1 Create `pyproject.toml` (hatchling, src layout, ruff/mypy/pytest config) and verify `pip install -e .[dev]` succeeds
- [ ] 1.2 Add `config.py` (pydantic-settings) and domain models in `models.py`; verify with `mypy src`

## 2. LLM providers

- [ ] 2.1 Define `LLMClient` protocol, `LLMResult`, `Source` and error types in `llm/base.py`; verify with `mypy src`
- [x] 2.2 Implement deterministic `MockLLMClient`; verify `tests/unit/test_llm_mock.py` passes
- [x] 2.3 Implement `AnthropicLLMClient` (effort, refusal fallback, web search, refusal handling, source extraction); verify `tests/unit/test_llm_anthropic.py` passes with a stubbed SDK client
- [x] 2.4 Add `build_llm_client(settings)` factory; verify provider selection test passes

## 3. Agents

- [x] 3.1 Implement Planner with tolerant JSON parsing and fallback; verify `tests/unit/test_agents.py::test_planner_*`
- [x] 3.2 Implement Researcher, Analyst, Critic and Writer; verify `tests/unit/test_agents.py`
- [x] 3.3 Implement Markdown report rendering with deduplicated, numbered sources; verify dedup test passes

## 4. Orchestration

- [x] 4.1 Implement orchestrator with bounded parallel research, partial-failure handling and revision loop; verify `tests/unit/test_orchestrator.py`
- [x] 4.2 Emit ordered progress events for every stage; verify event-order test passes
- [x] 4.3 Run a full offline end-to-end research with the mock provider; verify `tests/integration/test_pipeline_e2e.py`

## Workflow follow-up

- Archive this change with `openspec archive add-research-agent-pipeline` after merge-ready.
