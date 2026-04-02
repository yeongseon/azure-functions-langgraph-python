# AGENTS.md

## Purpose
`azure-functions-langgraph` deploys LangGraph compiled graphs as Azure Functions HTTP endpoints with zero boilerplate.

## Read First
- `README.md`
- `CONTRIBUTING.md`

## Working Rules
- Runtime code must remain compatible with Python 3.10+.
- Public APIs must be fully typed.
- Graph registration must remain protocol-based — accept any object satisfying `LangGraphLike`, not just `CompiledStateGraph`.
- Keep documentation examples, app behaviour, and tests synchronized.
- When bumping version, update `tests/test_public_api.py` to match the new version string.

## Validation
- `make test`
- `make lint`
- `make typecheck`
- `make build`
