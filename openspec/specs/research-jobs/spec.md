# research-jobs Specification

## Purpose
Defines how research jobs move through their lifecycle, how they are stored and queued, and
how worker processes execute them so the service scales horizontally and survives restarts.

## Requirements

### Requirement: Job lifecycle
A job SHALL move only through `queued → running → completed | failed | cancelled` (or
`queued → cancelled`), recording created, started and finished timestamps.

#### Scenario: Successful job
- **WHEN** a queued job is executed and the pipeline succeeds
- **THEN** its status becomes `running` then `completed`, with `started_at` and `finished_at` set

#### Scenario: Pipeline error
- **WHEN** the pipeline raises an error
- **THEN** the job status becomes `failed` with a human-readable error message

### Requirement: Shared state across processes
With the Redis backend, a job submitted through any API replica SHALL be visible to every
replica and executable by any worker process.

#### Scenario: Submit on one replica, read on another
- **WHEN** a job is created through one API process
- **THEN** a second API process using the same Redis returns the same job

### Requirement: Bounded worker concurrency
A worker SHALL execute at most the configured number of jobs concurrently, and each job SHALL
be executed by exactly one worker.

#### Scenario: Two workers, one job
- **WHEN** two workers poll the queue and one job is enqueued
- **THEN** exactly one worker executes it

### Requirement: Cancellation
Cancelling a queued job SHALL prevent its execution; cancelling a running job SHALL stop it at
the next stage boundary and mark it `cancelled`.

#### Scenario: Cancelled before pickup
- **WHEN** a job is cancelled while queued and a worker later dequeues it
- **THEN** the worker skips it and the status remains `cancelled`

### Requirement: Graceful shutdown
On SIGTERM a worker SHALL stop accepting new jobs and wait up to the configured grace period
for running jobs to finish.

#### Scenario: Shutdown during a job
- **WHEN** SIGTERM is received while a job is running and it completes within the grace period
- **THEN** the job is marked completed before the worker exits

### Requirement: Retention
Stored jobs and their events SHALL expire after the configured retention period.

#### Scenario: TTL applied
- **WHEN** a job is stored with the Redis backend
- **THEN** its keys carry an expiry equal to the configured retention period

### Requirement: Crash recovery
Workers SHALL publish a heartbeat, and live workers SHALL re-queue jobs held by a worker
whose heartbeat has expired so that those jobs run to completion on another worker. A
recovered job SHALL be visible to consumers only after its status is reset to `queued`.

#### Scenario: Worker crashes mid-job
- **WHEN** a worker dies while a job is `running` and another worker is alive
- **THEN** within a few heartbeat intervals the job is re-queued, a `job_queued` event records the recovery, and the job reaches `completed`

#### Scenario: Two workers recover concurrently
- **WHEN** two live workers scan for orphaned jobs at the same time
- **THEN** each orphaned job is re-queued exactly once
