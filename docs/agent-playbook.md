# Agent Playbook

## Source Of Truth
- [AGENTS.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/AGENTS.md) for repository scope, issue conventions, and automation rules.
- `README.md` for installation, quick start, and CLI examples.
- `CONTRIBUTING.md` for branch, commit, and release workflow.
- `pyproject.toml` and `Makefile` for supported commands.

## Repository Map
- `src/azure_functions_langgraph/` package code.
- `tests/` LangGraph adapter and deployment coverage.
- `examples/` example agent applications.
- `docs/` documentation site content.

## Change Workflow
1. Confirm whether the change affects LangGraph adapter API, agent lifecycle, state management, or docs only.
2. Update examples when they are used as public contract material.
3. Keep tests and docs in lockstep for public behavior changes.
4. Do not broaden supported Python versions or dependency ranges casually.
5. Record review evidence per [docs/review-evidence-policy.md](review-evidence-policy.md) in the PR's "Review Evidence" section before merging — advisory, not a required check.

## Validation
- `make test`
- `make lint`
- `make typecheck`
- `make security`
- `make build`
