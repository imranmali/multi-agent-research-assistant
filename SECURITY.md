# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x (latest minor) | Yes |
| < 1.0 | No |

Security fixes are released as patch versions and published as new container images.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately through
[GitHub Security Advisories](https://github.com/imranmali/multi-agent-research-assistant/security/advisories/new).

Include:

- the affected version or image tag
- a description of the issue and its impact
- steps to reproduce or a proof of concept

Never include real API keys. Rotate any key you may have exposed.

We aim to:

- acknowledge reports within **3 business days**
- give an initial assessment within **7 days**
- ship a fix for confirmed high or critical issues within **30 days**

After the fix is released, we credit reporters in the advisory unless you prefer to stay
anonymous.

## Scope

In scope:

- the API (authentication, input validation, SSE)
- the job queue and workers
- the container image
- the deployment manifests and CI/CD workflows in this repository

Out of scope:

- vulnerabilities in third-party services, such as the Anthropic API or your Redis provider
- content of reports generated from public web sources

## Hardening guidance for operators

- Set `API_KEYS` to long random values, rotate them, and serve the API only over TLS.
- Store `ANTHROPIC_API_KEY` and `API_KEYS` in a secret manager or Kubernetes Secret, never in
  images, ConfigMaps or logs.
- Run the provided image unmodified. It runs as non-root (uid 10001), and the manifests set a
  read-only root filesystem, drop all capabilities and use the `RuntimeDefault` seccomp profile.
- Keep Redis on a private network with authentication and TLS (`rediss://`).
- Restrict `CORS_ORIGINS` and `FORWARDED_ALLOW_IPS` to the values you need.
- Treat generated reports as untrusted content. They summarise arbitrary web pages.

Automated checks run on every change: CodeQL (Python) and Dependabot updates for pip, Docker and
GitHub Actions.
