# Tasks

## 1. Job persistence and queue

- [x] 1.1 Define `JobStore`/`JobQueue` protocols and `InMemoryJobStore`; verify `tests/unit/test_store_memory.py`
- [x] 1.2 Implement `RedisJobStore` and `RedisJobQueue` with TTLs; verify `tests/unit/test_store_redis.py` against fakeredis
- [x] 1.3 Implement `JobExecutor` (runs orchestrator, records events, cooperative cancel); verify `tests/unit/test_executor.py`

## 2. Execution modes

- [x] 2.1 Implement in-process `JobRunner` with bounded concurrency; verify runner tests
- [x] 2.2 Implement `research-worker` with bounded concurrency and graceful SIGTERM shutdown; verify `tests/unit/test_worker.py`

- [x] 2.3 Heartbeat-based recovery of jobs held by crashed workers; verify `tests/unit/test_recovery.py`

## 3. HTTP API

- [x] 3.1 Implement `create_app` with `/v1/research` submit/get/list/cancel and validation; verify `tests/api/test_research_routes.py`
- [x] 3.2 Implement SSE events endpoint; verify stream test receives ordered events and terminates
- [x] 3.3 Add API-key auth, request id middleware, `/healthz`, `/readyz`, `/metrics`; verify `tests/api/test_ops.py`
- [x] 3.4 Structured JSON logging; verify log output contains request id in tests

## 4. CLI and integration

- [x] 4.1 Implement `research-assistant` CLI (`run`, `serve`, `worker`); verify `tests/unit/test_cli.py`
- [x] 4.2 End-to-end API test with mock provider (submit → complete → fetch report); verify `tests/integration/test_api_e2e.py`
- [x] 4.3 Cross-replica test: two app instances sharing fakeredis plus a worker; verify `tests/integration/test_redis_scaleout.py`

## Workflow follow-up

- Archive this change with `openspec archive add-research-service-api` after merge-ready.
