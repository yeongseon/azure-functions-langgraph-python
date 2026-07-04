# Changelog

All notable changes to this project will be documented here.

This project follows [Conventional Commits](https://www.conventionalcommits.org/) and [Keep a Changelog](https://keepachangelog.com/) conventions.

For the full changelog, see [CHANGELOG.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CHANGELOG.md) in the repository root.

## Unreleased

### Breaking Changes

- **`LangGraphApp(auth_level=...)` now defaults to `AuthLevel.FUNCTION`** (was `AuthLevel.ANONYMOUS`). Deployed endpoints require a function key by default. Existing code that relied on the anonymous default must pass `auth_level=func.AuthLevel.ANONYMOUS` explicitly. (#240)
- Opting into `AuthLevel.ANONYMOUS` at the app level now emits an **unconditional** `UserWarning` (previously only when `AZURE_FUNCTIONS_ENVIRONMENT` was set). The warning fires in tests, CI, and local runs so accidental anonymous deployments are always loud. The environment-gated codepath and the `os`/`logging` imports it required have been removed. (#240)

### Added

- Pluggable `ThreadLock` protocol under `azure_functions_langgraph.locks` (`ThreadLock`, `InProcessThreadLock`, `AzureBlobLeaseThreadLock`) — swap the native invoke/stream thread lock for a distributed backend in multi-instance deployments. (#241)
- `LangGraphApp(thread_lock=...)` argument (defaults to `InProcessThreadLock`, preserving previous behaviour). (#241)
- `AZFUNC_LANGGRAPH_LOCK_BACKEND` environment variable as a fail-fast safety guard: setting it to `distributed` (or any non-empty value other than `inprocess`) while `thread_lock` resolves to `InProcessThreadLock` raises `RuntimeError` at construction, catching multi-instance deployments that forgot to wire a distributed backend. (#241)

### Changed

- README banner across all locales (`README.md`, `README.ko.md`, `README.ja.md`, `README.zh-CN.md`) now says **Alpha Notice** to match the existing `Development Status :: 3 - Alpha` classifier in `pyproject.toml`. Documentation now explicitly points at the classifier as the source of truth for maturity. (#242)
- `docs/{security,production-guide,configuration,faq,installation}.md` updated to reflect the FUNCTION default and the ANONYMOUS opt-in warning. (#240)
- `examples/openapi_bridge`, `examples/platform_compat_sdk`, `examples/sqlite_checkpoint_local`, `examples/persistent_agent_blob_table` now include inline comments explaining the ANONYMOUS opt-in and its unconditional warning. (#240)
- `handle_invoke` / `handle_stream` in `_handlers.py` now require an explicit `thread_lock: ThreadLock` keyword argument (previously module-level `_thread_locks`); `LangGraphApp` wires this automatically. External callers that invoked handlers directly must pass `thread_lock=self.thread_lock`. (#241)

### Documentation

- README (en/ja/zh-CN): new *Distributed thread locking* subsection with backend matrix and safety-guard guidance. (#241)
- `docs/production-guide.md`: new *Distributed thread locking* section under *Concurrency & Scale*, with scale-out matrix and interaction notes for Platform-compatible runs. (#241)

### Migration

```python
# Before (implicit ANONYMOUS default — no auth required)
app = LangGraphApp()

# After (implicit FUNCTION default — function key required)
app = LangGraphApp()

# To preserve the old behavior explicitly (emits UserWarning):
app = LangGraphApp(auth_level=func.AuthLevel.ANONYMOUS)
```

## 0.5.0 (2026-04-07)

### Breaking Changes

- Removed deprecated `GET /api/openapi.json` endpoint and internal `_build_openapi()` method. Use `azure-functions-openapi-python` with `register_with_openapi()` bridge instead.

### Added

- Metadata API with immutable dataclass-based snapshots (`GraphMetadata`, `AppMetadata`)
- OpenAPI bridge module for `azure-functions-openapi-python` ecosystem integration
- `CloneableGraph` protocol for explicit clone support in threadless runs
- SDK compatibility policy and contract tests
- Production hardening guide (auth, observability, timeouts, concurrency, storage)
- Step-by-step deployment guide with real Azure-verified output
- Choose-a-plan hosting guide for developers new to Azure Functions
- 695+ tests, 91%+ coverage

### Changed

- Production warning when `auth_level=ANONYMOUS` in non-local environments
- Platform routes split into `threads.py`, `runs.py`, `assistants.py` resource modules
- All documentation rewritten for general-developer friendliness

### Fixed

- Mermaid fence format for correct rendering
- Bandit B311 false positive
- MkDocs strict-mode nav, anchor, and link failures

## 0.4.0 (2026-04-05)

### Added

- Thread update, delete, search, and count endpoints
- Assistants count endpoint
- Threadless runs (stateless execution without thread)
- Thread state update and history endpoints
- Azure Blob Storage checkpointer (`AzureBlobCheckpointSaver`)
- Azure Table Storage thread store (`AzureTableThreadStore`)
- Persistent storage integration tests
- 645 tests, 91% coverage

## 0.3.0 (2026-04-05)

### Added

- Platform API compatibility layer (full `langgraph-sdk`-compatible HTTP surface)
- Thread store protocol and in-memory implementation
- SSE streaming with platform event sequence
- Input validation and request size limits
- SDK compatibility tests
- 427 tests, 96% coverage

## 0.2.0 (2026-04-05)

### Added

- State endpoint for retrieving thread state
- Per-graph auth level override
- Release automation
- 105 tests, 98%+ coverage

## 0.1.0a0 (2026-04-02)

### Added

- Initial release with `LangGraphApp`, invoke/stream/health endpoints
- Protocol-based graph registration
- Pydantic v2 contracts
- Example simple agent
