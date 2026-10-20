# Design

## Context

Depends on `add-research-agent-pipeline` (orchestrator + `on_event` callback). See
proposal.md for motivation. Deployment target is containers (Docker / Kubernetes).

## Goals / Non-Goals

**Goals:**
- Stateless API replicas; all job state in a `JobStore`.
- Same code path in dev (in-process runner) and prod (Redis queue + workers).
- Observable: request ids, JSON logs, Prometheus metrics, readiness checks.

**Non-Goals:**
- Exactly-once execution across worker crashes (at-most-once with visibility is acceptable for v1).
- Multi-tenant authorization.

## Decisions

1. **`JobStore` protocol** (`create/get/update/list/append_event/events/request_cancel/...`)
   with `InMemoryJobStore` and `RedisJobStore`. Redis layout: `job:{id}` (JSON string),
   `job:{id}:events` (list), `jobs:index` (sorted set by created time), all with TTL.
2. **Queue = Redis list with `BLMOVE` into a per-worker processing list**; workers ack by
   removing the item. Simple, no extra broker. Alternative: Celery/arq — rejected to keep the
   dependency surface small and fully async.
3. **Two execution modes** selected by `JOB_BACKEND`:
   - `memory`: API process runs jobs with an in-process `JobRunner` (asyncio tasks + semaphore).
   - `redis`: API only enqueues; `research-worker` processes execute.
4. **SSE via polling the event list** (`events(since=n)`) every 0.5s — works identically for
   both backends and across replicas without pub/sub fan-out complexity.
5. **Cancellation is cooperative**: a `cancel_requested` flag on the job is checked by the
   orchestrator's event hook at every stage boundary (raises `JobCancelled`).
6. **Auth**: static API keys from `API_KEYS` (comma separated), compared in constant time.
7. **App factory** `create_app(settings, store=None, runner=None)` for dependency injection in tests.

## Risks / Trade-offs

- [Worker crash loses in-flight job] → job stays `running`; processing list allows a reaper
  to re-queue (documented follow-up); jobs have TTL so they never leak.
- [SSE polling adds Redis load] → 0.5s interval and per-connection limits; fine for v1 scale.
- [In-memory backend is single-process only] → `/readyz` and docs make it explicit; prod uses Redis.

## Migration Plan

New service; no migration. Rollback = redeploy previous image. Redis data is disposable (TTL).
