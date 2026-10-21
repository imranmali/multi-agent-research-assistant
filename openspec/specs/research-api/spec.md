# research-api Specification

## Purpose
Defines the versioned HTTP interface for submitting research questions, observing their
progress and retrieving reports, plus the operational endpoints used by orchestrators.

## Requirements

### Requirement: Submit research
The API SHALL accept `POST /v1/research` with a JSON body containing a `question` (3–2000
characters) and optional limits, and SHALL respond `202 Accepted` with the job id and status
`queued`.

#### Scenario: Valid submission
- **WHEN** a client posts `{"question": "What is the state of solid-state batteries?"}`
- **THEN** the response is 202 with a JSON body containing `id` and `status` = `queued`

#### Scenario: Invalid submission
- **WHEN** a client posts a question shorter than 3 characters or limits outside allowed ranges
- **THEN** the response is 422 and no job is created

### Requirement: Read job
The API SHALL return the job's status, timestamps, error (if any) and report (when completed)
at `GET /v1/research/{id}`, and SHALL return 404 for unknown ids.

#### Scenario: Completed job
- **WHEN** a client fetches a completed job
- **THEN** the body contains `status` = `completed` and a `report` with `markdown` and `sources`

#### Scenario: Unknown job
- **WHEN** a client fetches an id that does not exist
- **THEN** the response is 404

### Requirement: Progress streaming
The API SHALL expose `GET /v1/research/{id}/events` as a Server-Sent Events stream that replays
past progress events and then follows new ones until the job reaches a terminal state.

#### Scenario: Stream ends at completion
- **WHEN** a client subscribes to a job that then completes
- **THEN** the client receives all progress events in order and the stream closes after the terminal event

### Requirement: List and cancel
The API SHALL list recent jobs at `GET /v1/research` (bounded page size) and SHALL cancel a
queued or running job at `DELETE /v1/research/{id}`, responding 409 for jobs already finished.

#### Scenario: Cancel queued job
- **WHEN** a client cancels a queued job
- **THEN** the response is 202 and the job ends with status `cancelled`

#### Scenario: Cancel finished job
- **WHEN** a client cancels a completed job
- **THEN** the response is 409

### Requirement: API key authentication
When API keys are configured the API SHALL require a valid `X-API-Key` header on all `/v1`
routes and SHALL reject missing or invalid keys with 401; operational endpoints stay public.

#### Scenario: Missing key
- **WHEN** keys are configured and a request to `/v1/research` has no `X-API-Key`
- **THEN** the response is 401

#### Scenario: Health without key
- **WHEN** keys are configured and a probe calls `/healthz`
- **THEN** the response is 200

### Requirement: Operational endpoints
The API SHALL expose `/healthz` (process alive), `/readyz` (dependencies such as the job store
reachable; 503 otherwise) and `/metrics` in Prometheus text format, and SHALL return an
`X-Request-ID` header on every response.

#### Scenario: Store unavailable
- **WHEN** the job store cannot be reached
- **THEN** `/readyz` responds 503 while `/healthz` still responds 200
