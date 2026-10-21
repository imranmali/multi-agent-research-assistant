# Tasks

## 1. Container packaging

- [x] 1.1 Multi-stage non-root `Dockerfile` with healthcheck and `.dockerignore`; verify `docker build` succeeds
- [x] 1.2 `docker-compose.yml` (api, worker, redis); verify `docker compose config` is valid

## 2. Continuous integration

- [x] 2.1 `ci.yml`: lint, format, mypy, pytest matrix + coverage, openspec validate, docker build + smoke test; verify green on the pull request
- [x] 2.2 Dependabot and CodeQL configuration; verify files parse (actionlint/yamllint)

## 3. Continuous delivery

- [x] 3.1 `release.yml`: GHCR publish on `main` (sha, latest) and on `v*` tags (semver) + GitHub Release; verify workflow syntax with actionlint
- [x] 3.2 Kubernetes manifests (namespace, configmap, api/worker deployments, service, HPA, PDB, redis); verify with `kubectl apply --dry-run=client` or kubeconform

## 4. Documentation and licensing

- [x] 4.1 README (architecture, quickstart, API, config, deployment), CONTRIBUTING, SECURITY, CHANGELOG; verify links/commands by running the quickstart
- [x] 4.2 MIT `LICENSE`, PR and issue templates; verify files present

## Workflow follow-up

- Archive this change with `openspec archive add-delivery-pipeline` after merge-ready.
