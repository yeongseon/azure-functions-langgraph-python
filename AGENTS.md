# AGENTS.md

## Purpose
`azure-functions-langgraph` deploys LangGraph compiled graphs as Azure Functions HTTP endpoints with zero boilerplate.

## Read First
- `README.md`
- `CONTRIBUTING.md`

## Working Rules

### Test Coverage
- Maintain test coverage at **95% or above** for committed changes and PRs.
- Run `hatch run pytest --cov --cov-report=term-missing -q` to verify before submitting changes.
- Any PR that drops coverage below 95% must include additional tests to compensate.
- Runtime code must remain compatible with Python 3.10+.
- Public APIs must be fully typed.
- Graph registration must remain protocol-based — accept any object satisfying `LangGraphLike`, not just `CompiledStateGraph`.
- Keep documentation examples, app behaviour, and tests synchronized.
- When bumping version, update `tests/test_public_api.py` to match the new version string.

## Issue Conventions

Follow these conventions when opening issues so the backlog stays consistent with sibling DX Toolkit repositories.

### Title

- Use Conventional Commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:`, `perf:`.
- Add a scope qualifier when it narrows the area: `feat(graph):`, `docs(adapter):`, `refactor(runtime):`.
- Keep the title imperative, under ~80 characters, no trailing period.
- Do **not** put `[P0]` / `[P1]` / `[P2]` (or any priority marker) in the title — priority lives in the body.

### Body

Use the following sections, in order, omitting any that do not apply:

```
## Priority: P0 | P1 | P2 (target vX.Y.Z, optional)

## Context
What problem this issue addresses and why now.

## Acceptance Checklist
- [ ] Concrete, verifiable items.

## Out of scope
- Items intentionally excluded, with links to the issues that track them.

## References
- PRs, ADRs, sibling issues, external docs.
```

### Labels

- Apply at least one of `bug`, `enhancement`, `documentation`, `chore`.
- Add `area:*` labels when they exist in the repository.
- Use `blocker` only when the issue blocks a release.

### Umbrella issues

When splitting a large piece of work into focused issues, keep the umbrella open as a tracker that links each child issue with a checkbox; close it once every child is closed or explicitly deferred.

## Validation
- `make test`
- `make lint`
- `make typecheck`
- `make build`
