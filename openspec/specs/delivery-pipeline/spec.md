# delivery-pipeline Specification

## Purpose
Defines the guarantees the repository gives about verifying, packaging and publishing every
change to the research assistant so that `main` is always releasable.

## Requirements

### Requirement: Pull request quality gates
Every pull request SHALL run lint, format check, type check, unit and integration tests with a
coverage threshold, OpenSpec validation and a container build, and SHALL be mergeable only when
all succeed.

#### Scenario: Failing test blocks merge
- **WHEN** a pull request introduces a failing test
- **THEN** the CI workflow reports failure for that pull request

### Requirement: Offline CI
CI SHALL NOT require LLM credentials or outbound LLM calls.

#### Scenario: No secrets configured
- **WHEN** CI runs on a fork without repository secrets
- **THEN** all test jobs still run and pass using the mock provider

### Requirement: Published images
Every push to `main` SHALL publish a container image tagged with the commit SHA, and every
`v*` tag SHALL publish semver-tagged images and a GitHub Release.

#### Scenario: Release tag
- **WHEN** tag `v1.2.3` is pushed
- **THEN** images tagged `1.2.3` and `1.2` are published and a release named `v1.2.3` is created

### Requirement: Hardened runtime image
The container image SHALL run as a non-root user, declare a health check, and start the API by
default with the worker selectable by command.

#### Scenario: Container smoke test
- **WHEN** CI starts the built image with the mock provider
- **THEN** `/healthz` responds 200 within 30 seconds
