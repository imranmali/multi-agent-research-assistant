# Proposal

## Why

The research pipeline takes minutes per question, so it cannot be served as a blocking HTTP
call, and a single process cannot absorb production load. Clients need a stable API to submit
research, poll or stream progress, and fetch results, while operators need to scale request
handling and research execution independently.

## What Changes

- REST API (FastAPI, versioned under `/v1`) to submit research jobs, read job status/result,
  stream progress (Server-Sent Events), list and cancel jobs.
- Asynchronous job model: submissions return `202 Accepted` with a job id; work is executed by
  a job runner.
- Pluggable job store and queue: in-memory (single process, dev/tests) and Redis (shared state,
  horizontally scaled API replicas and worker processes).
- Standalone worker process (`research-worker`) that consumes the Redis queue with bounded
  concurrency and graceful shutdown.
- Operational endpoints: liveness `/healthz`, readiness `/readyz`, Prometheus `/metrics`.
- Optional API-key authentication, request-size limits, structured JSON logging with request ids.
- CLI (`research-assistant`) to run research locally or start the API.

Out of scope: user accounts / multi-tenancy, billing, a web UI, and result search.

## Capabilities

### New Capabilities
- `research-api`: The HTTP contract clients use to submit and observe research, including
  authentication, validation and operational endpoints.
- `research-jobs`: Lifecycle, persistence, queuing, cancellation and worker execution of
  research jobs across multiple processes.

### Modified Capabilities
<!-- none -->

## Impact

- New modules `api/`, `jobs/` (store, queue, runner, worker), `cli.py`, `logging.py`, `metrics`.
- New dependencies: `fastapi`, `uvicorn`, `redis`, `prometheus-client`, `structlog`; dev: `fakeredis`, `httpx`.
- New env vars: `JOB_BACKEND`, `REDIS_URL`, `API_KEYS`, `WORKER_CONCURRENCY`, `JOB_TTL_SECONDS`.
