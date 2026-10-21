## Summary

<!-- What does this PR change and why? Link the issue it closes. -->

Closes #

## OpenSpec

<!-- Behaviour changes go through a spec change first (see CONTRIBUTING.md). -->

- Change: `openspec/changes/<change-name>/` <!-- or "n/a: docs / chore / refactor without behaviour change" -->
- [ ] Tasks in `tasks.md` are ticked for the work in this PR
- [ ] `openspec validate --all --strict` passes

## Type of change

- [ ] `feat`: new capability
- [ ] `fix`: bug fix
- [ ] `docs`: documentation only
- [ ] `ci` / `build`: pipeline, packaging or deployment
- [ ] `refactor` / `perf` / `test` / `chore`
- [ ] Breaking change (API contract, env vars or deployment manifests)

## How was this tested?

<!-- Commands you ran, new tests, manual checks (e.g. a run with LLM_PROVIDER=anthropic). -->

## Checklist

- [ ] PR title follows [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat(api): add job listing`)
- [ ] `make lint typecheck test` passes locally (coverage >= 85%)
- [ ] Tests do not need network access or API keys (mock provider / fakeredis)
- [ ] Docs updated (README, `.env.example`, `CHANGELOG.md` under *Unreleased*) where relevant
- [ ] No secrets, keys or personal data in code, logs or fixtures
