# Changelog

All notable changes to this project will be documented here.

This project follows [Conventional Commits](https://www.conventionalcommits.org/) and [Keep a Changelog](https://keepachangelog.com/) conventions.

For the full changelog, see [CHANGELOG.md](https://github.com/yeongseon/azure-functions-langgraph/blob/main/CHANGELOG.md) in the repository root.

## 0.1.0a0 (Unreleased)

### Added

- `LangGraphApp` class for deploying compiled LangGraph graphs as Azure Functions HTTP endpoints
- `POST /api/graphs/{name}/invoke` endpoint for synchronous graph invocation
- `POST /api/graphs/{name}/stream` endpoint for buffered SSE streaming
- `GET /api/health` endpoint listing registered graphs with checkpointer status
- Protocol-based graph acceptance (`InvocableGraph`, `StreamableGraph`, `LangGraphLike`)
- Pydantic v2 request/response validation (`InvokeRequest`, `StreamRequest`, `InvokeResponse`, etc.)
- Support for invoke-only graphs (stream endpoint returns 501)
- Checkpointer pass-through via LangGraph config
- Configurable Azure Functions auth level
