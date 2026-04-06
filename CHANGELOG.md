# Changelog

All notable changes to this project will be documented in this file.

## [0.5.0] - 2026-04-07

### Breaking Changes

- Removed deprecated `GET /api/openapi.json` endpoint and internal `_build_openapi()` method. Use `azure-functions-openapi` with `register_with_openapi()` bridge instead (#99, PR #113).

### Added

- **Metadata API**: Immutable dataclass-based metadata (`GraphMetadata`, `AppMetadata`) with deep-frozen snapshot semantics (#87)
- **OpenAPI bridge module**: `azure_functions_langgraph.openapi` integration module for `azure-functions-openapi` ecosystem interop (#87)
- **`CloneableGraph` protocol**: Explicit protocol for graphs supporting `clone()`, refactored `_get_threadless_graph` for cleaner threadless run support (#95, #96)
- **SDK compatibility policy**: Formal version compatibility matrix and contract tests for `langgraph-sdk` (#91, PR #102)
- **Production hardening guide**: Auth, observability, timeouts, concurrency, and storage configuration (#105)
- **Deployment guide**: Step-by-step Azure deployment with real verified output for all plans (#73, PR #108)
- **Choose-a-plan guide**: Hosting plan comparison for developers new to Azure Functions
- **Azure deployment verification**: Real Azure deployment outputs verified and documented (#110, #111)
- 695+ tests total, 91%+ coverage

### Changed

- **Auth level tightened**: Production warning when `auth_level=ANONYMOUS` in non-local environments (#97)
- **Platform routes modularized**: Split monolithic `platform/routes.py` into `threads.py`, `runs.py`, `assistants.py` resource modules (#89, PR #101)
- **Documentation rewrite**: All docs rewritten for general-developer friendliness with step-by-step instructions (#115)
- Concurrency constraints, scale envelopes, and SSE buffering behavior documented (#90, #92, #93, #98, #100, PR #103)

### Fixed

- Mermaid fence format switched to `fence_div_format` for correct rendering (#81)
- Bandit B311 false positive suppressed for non-security random usage (#88)
- MkDocs strict-mode failures: nav entries, anchor slugs, and out-of-docs links (#116, PR #117)
- Deep immutability enforced on metadata snapshots (Oracle review follow-up)

### Documentation

- README restructured for ecosystem positioning (#84, PR #85)
- DESIGN.md updated with architecture accuracy fixes and new ADRs (#72, #75, #77)
- MkDocs Mermaid rendering standardized with pinned JS version (#78, #79)
- Deployment docs include real Azure CLI output and troubleshooting tables
- Full i18n updates (ko/en) for OpenAPI removal and deployment guides

## [0.4.0] - 2026-04-05

### Added

- **Thread update and delete**: `PATCH /threads/{thread_id}` with shallow-merge metadata update, `DELETE /threads/{thread_id}` soft-delete (#54, PR #64)
- **Thread search and count**: `POST /threads/search` with status/metadata filtering + limit/offset pagination, `POST /threads/count` (#55, PR #65)
- **Assistants count**: `POST /assistants/count` endpoint (#56, PR #63)
- **Threadless runs**: `POST /runs/wait` and `POST /runs/stream` without `thread_id` — stateless execution with cloned graph (`checkpointer=None`) (#53, PR #66)
- **Thread state update**: `POST /threads/{thread_id}/state` — inject values as a graph node via `update_state()` (#57, PR #67)
- **Thread state history**: `POST /threads/{thread_id}/history` — retrieve checkpoint history with `limit`/`before` filtering (#58, PR #67)
- **Azure Blob Storage checkpointer**: `AzureBlobCheckpointSaver` for durable checkpoint persistence, optional extra `azure-blob` (#60, PR #68)
- **Azure Table Storage thread store**: `AzureTableThreadStore` for durable thread metadata persistence, optional extra `azure-table` (#59, PR #69)
- **Persistent storage integration tests**: end-to-end tests verifying Platform API with both in-memory and Azure-mocked backends, including restart simulation (#61, PR #70)
- `UpdatableStateGraph` protocol for graphs supporting `update_state()`
- `StateHistoryGraph` protocol for graphs supporting `get_state_history()`
- Platform API coverage: 7 → ~18 of 50 LangGraph Platform endpoints
- 645 tests total, 91% coverage

### Changed

- Thread metadata update uses **shallow merge** semantics (not replace)
- Threadless run endpoints strip client-supplied `thread_id` from config
- Optional Azure dependencies: `pip install azure-functions-langgraph[azure-blob]` or `[azure-table]`

### Documentation

- Updated README with persistent storage quickstart and new endpoint list
- Updated DESIGN.md with `checkpointers/`, `stores/` subpackage architecture and new ADRs
## [0.3.0] - 2026-04-05

### Added

- **Platform API compatibility layer**: Full `langgraph-sdk`-compatible HTTP API surface (#35–#42)
- `platform/` subpackage with Pydantic contracts, route layer, SSE streaming, and thread store (#35, #36)
- `ThreadStore` protocol + `InMemoryThreadStore` for thread lifecycle management (#37)
- Platform API routes: assistants (search, get), threads (create, get, state), runs (wait, stream) (#38)
- Platform-compatible SSE streaming with `metadata → values → end` event sequence (#39)
- Input validation and request size limits: body size, JSON depth, node count, thread_id format (#40)
- Integration tests with real `StateGraph` + `MemorySaver` graphs (#41)
- SDK compatibility tests using real `langgraph_sdk.SyncLangGraphClient` via `httpx.MockTransport` bridge (#42)
- 427 tests total, 96% coverage

### Changed

- Handlers extracted from `app.py` into `_handlers.py` for native routes (#35)
- Transport-agnostic validators in `_validation.py` (#40)
- Unsupported features (interrupt_before/after, webhook, multi-stream-mode) return 501 with clear messages (#39, #40)

### Documentation

- Updated README with platform compat quickstart and architecture diagram (#30)
- Added DESIGN.md documenting architecture, design decisions, and ADRs (#43)
- Full i18n documentation (ko/en) (#43)
## [0.2.0] - 2026-04-05

### Added

- State endpoint: `GET /api/graphs/{name}/threads/{thread_id}/state` for retrieving thread state from stateful graphs (#17)
- Per-graph auth level override via `register(graph, name, auth_level=...)` (#18)
- `StateResponse` contract model for state endpoint responses
- `StatefulGraph` protocol for graphs that support `get_state()`
- `StateResponse` and `StatefulGraph` exported in public API `__all__`
- Release automation: auto GitHub Release on tag push via `release.yml` workflow (#22)
- 105 tests total, 98%+ coverage with `fail_under=90` (#19)

### Changed

- `__all__` cleaned up with proper re-exports of all contracts and protocols from package root (#21)
- CI/Release automation validated and hardened (#20)
- `git-cliff` pinned to `>=2.12,<3` in release workflow

### Fixed

- Narrowed exception handling in `_handle_state`: `KeyError`/`ValueError` → 404, unexpected errors → 500

## [0.1.0a0] - 2026-04-02

### Added

- Initial package layout and scaffolding
- `LangGraphApp` class for registering compiled graphs as Azure Functions endpoints
- Protocol-based interfaces (`InvocableGraph`, `StreamableGraph`, `LangGraphLike`)
- Pydantic v2 request/response contracts
- Invoke endpoint (`POST /api/graphs/{name}/invoke`)
- Stream endpoint (`POST /api/graphs/{name}/stream`) with buffered SSE
- Health endpoint (`GET /api/health`)
- Example simple agent (`examples/simple_agent/`)
- Comprehensive test suite (25+ tests)
- Public API surface tests
<!-- generated by git-cliff -->
