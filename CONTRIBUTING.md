# Contributing

Thanks for helping improve the Multi-Agent Research Assistant! This guide covers how changes
move from idea to `main`. By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules

- `main` is protected and always releasable. All changes land through pull requests with green
  CI and an approving review from a [code owner](.github/CODEOWNERS).
- Behaviour changes are **spec-driven**: they start as an OpenSpec change (see below).
- Tests must not need network access or API keys. Use the `mock` LLM provider and `fakeredis`.
- Never commit secrets. `.env` is git-ignored; document new settings in `.env.example`.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
make install                      # pip install -e ".[dev]"
npm install -g @fission-ai/openspec@1.14.3   # for spec validation
make check                        # lint, typecheck, tests + coverage, openspec validate
```

Docker is only needed for `make docker-build` and `make compose-up`.

## Spec-driven workflow (OpenSpec)

1. **Propose.** Create `openspec/changes/<verb-noun>/` with `proposal.md` (why, what, and what
   is out of scope), `design.md` (decisions), `specs/<capability>/spec.md` (requirements with
   `#### Scenario:` WHEN/THEN blocks) and `tasks.md` (each task states how it is verified).
   - `openspec new change <name>`, or `/opsx:propose` in Claude Code
   - `openspec validate <name> --strict`
   - Small fixes, docs and CI-only changes do not need a spec change.
2. **Apply.** Implement the tasks and tick each checkbox once its verification passes
   (`/opsx:apply`). Spec, code and tests travel in the same pull request.
3. **Archive.** After merge, run `openspec archive <name>` (`/opsx:archive`) to fold the deltas
   into `openspec/specs/`, and submit that in a follow-up `docs:` or `chore:` PR.

## Branches and commits

- Branch from `main` using a short-lived, prefixed branch: `feat/…`, `fix/…`, `docs/…`, `ci/…`,
  `refactor/…`, `test/…` or `chore/…`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages and the PR
  title: `type(scope): summary`. Types are `feat`, `fix`, `docs`, `ci`, `build`, `refactor`,
  `perf`, `test` and `chore`. Mark breaking changes with `!`, for example `feat(api)!: …`.
- Keep PRs focused. Rebase on `main` rather than merging `main` in. PRs are squash-merged.

## Pull request checklist

- [ ] `make check` passes locally: ruff, mypy, pytest with at least 85% coverage, and openspec
      validate.
- [ ] New behaviour has tests: unit tests in `tests/unit`, HTTP tests in `tests/api`, and
      end-to-end tests in `tests/integration` (marked `@pytest.mark.integration`).
- [ ] The README, `.env.example` and `CHANGELOG.md` (under *Unreleased*) are updated when you
      change behaviour or configuration.
- [ ] The CI workflow is green: lint, tests on 3.11, 3.12 and 3.13, openspec, and the Docker
      smoke test.

## Code style

- Format and lint with `ruff` (line length 100). Type-check with strict `mypy` on `src/`.
- Keep the code async end to end. Do not add blocking IO in the API or workers.
- Agents depend on the `LLMClient` protocol, never on the Anthropic SDK directly.

## Releasing (maintainers)

1. Move the *Unreleased* entries in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading,
   bump `version` in `pyproject.toml`, and merge.
2. Tag the release on `main`: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `release.yml` publishes `ghcr.io/imranmali/multi-agent-research-assistant:X.Y.Z` and `X.Y`,
   then creates the GitHub Release. If `KUBE_CONFIG` is set, it also deploys to `production`.
