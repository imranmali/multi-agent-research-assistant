# Proposal

## Why

A production service needs every change gated by automated quality checks and a repeatable,
auditable way to build and ship it. Without CI, regressions reach `main`; without a release
pipeline and deployment manifests, every deployment is hand-made.

## What Changes

- GitHub Actions **CI** on every pull request and push to `main`: ruff lint + format check,
  mypy, pytest with coverage gate on Python 3.11–3.13, OpenSpec validation, Docker image build
  and container smoke test.
- GitHub Actions **CD**: on `main`, build and publish a multi-arch image to GHCR tagged with
  the commit SHA and `latest`; on `v*` tags, publish semver tags and create a GitHub Release.
- Hardened multi-stage **Dockerfile** (non-root, healthcheck), **docker-compose** stack
  (api + worker + redis), **Kubernetes** manifests (Deployments, Service, HPA, PDB, probes).
- Dependabot for pip, Docker and Actions; CodeQL security scanning.
- Project docs: README, CONTRIBUTING, SECURITY, CHANGELOG, MIT LICENSE, PR / issue templates.

Out of scope: provisioning cloud infrastructure (Terraform), Helm charts, secrets management
beyond Kubernetes Secrets references.

## Capabilities

### New Capabilities
- `delivery-pipeline`: The guarantees the repository gives about verifying, packaging and
  publishing every change.

### Modified Capabilities
<!-- none -->

## Impact

- New `.github/` workflows and templates, `Dockerfile`, `docker-compose.yml`, `deploy/k8s/`,
  root documentation files and `LICENSE`.
