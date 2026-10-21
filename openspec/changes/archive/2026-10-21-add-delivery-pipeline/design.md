# Design

## Context

See proposal.md. Repository hosted on GitHub; images go to GHCR (`ghcr.io/<owner>/<repo>`),
which needs no extra secrets (uses `GITHUB_TOKEN`).

## Goals / Non-Goals

**Goals:** fast feedback (<10 min CI), reproducible builds, least-privilege workflow tokens.
**Non-Goals:** environment-specific deployment automation (cluster credentials are org specific);
manifests are provided and applied by the operator or a GitOps tool.

## Decisions

1. **Separate `ci.yml` and `release.yml`** so publishing never runs on untrusted PRs.
2. **Matrix on Python 3.11/3.12/3.13**, coverage gate (`--cov-fail-under=85`) on one leg.
3. **Docker Buildx with GHA cache**; multi-arch (amd64, arm64) only on publish.
4. **Container smoke test in CI** runs the image with `LLM_PROVIDER=mock` and curls `/healthz`.
5. **Kubernetes**: separate `api` and `worker` Deployments sharing one image, HPA on CPU for
   both, PDB, readiness on `/readyz`, liveness on `/healthz`, secrets via `secretRef`.
6. **Permissions**: workflows default to `contents: read`; publish job adds `packages: write`.

## Risks / Trade-offs

- [Multi-arch builds are slow] → only on `main`/tags; PRs build amd64.
- [HPA on CPU is a weak signal for IO-bound workers] → documented; queue-depth autoscaling (KEDA) is a follow-up.
