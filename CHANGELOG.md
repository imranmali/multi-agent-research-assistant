# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-20

### Added

- Multi-agent research pipeline:
  - Planner, parallel Researchers, Analyst, Critic with a bounded revision loop, and Writer.
  - Researchers use Claude server-side web search.
  - Output is a cited Markdown report with a numbered, deduplicated source list.
- LLM providers:
  - Anthropic Claude (`claude-opus-5-5`) through the official `anthropic` SDK, with configurable
    effort, retries, timeouts and server-side refusal fallbacks.
  - A deterministic offline `mock` provider.
- Ordered progress events for every pipeline stage.
- Graceful handling of planner, critic and per-sub-question failures and of model refusals.
- Versioned REST API:
  - `POST /v1/research`, `GET /v1/research/{id}`, `GET /v1/research` and
    `DELETE /v1/research/{id}`.
  - Server-Sent Events progress stream at `GET /v1/research/{id}/events`.
- Optional `X-API-Key` authentication and `X-Request-ID` correlation.
- Operational endpoints: `/healthz`, `/readyz` and the Prometheus `/metrics`.
- Job system:
  - An in-memory backend for development, and a Redis store and queue for horizontally scaled
    API replicas.
  - `research-worker` processes with bounded concurrency, cooperative cancellation, graceful
    SIGTERM shutdown and TTL-based retention.
- `research-assistant` CLI (`run`, `serve`, `worker`).
- Structured JSON logging with request and job ids.
- Multi-stage, non-root Docker image with a health check, and a Docker Compose stack (Redis,
  API and 2 workers).
- Kubernetes manifests (Kustomize): API and worker Deployments, Service, HPA, PDB, probes,
  hardened security contexts, a Redis StatefulSet and an optional Ingress.
- GitHub Actions:
  - CI: ruff, mypy, pytest on 3.11, 3.12 and 3.13 with an 85% coverage gate, OpenSpec
    validation, and a Docker smoke test.
  - Release: a multi-arch GHCR image with SBOM and provenance, a GitHub Release, and an
    optional Kubernetes deploy.
- CodeQL and Dependabot.
- Project documentation: README, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, PR and issue templates,
  and the MIT license.

[Unreleased]: https://github.com/imranmali/multi-agent-research-assistant/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/imranmali/multi-agent-research-assistant/releases/tag/v1.0.0
