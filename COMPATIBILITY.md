# Compatibility Policy

## LangGraph Version Support

| Package | Supported Versions | Notes |
|---|---|---|
| `langgraph` | `>=0.2` | Runtime dependency. Tested with 0.2.x in CI. |
| `langgraph-sdk` | `~0.1` | Platform compat layer mirrors this SDK version's REST API shapes. |
| `pydantic` | `>=2.0` | Required for request/response models. |
| `azure-functions` | `>=1.17` | Azure Functions Python v2 programming model. |

## Platform Compatibility Layer

The `platform/` subpackage mirrors the LangGraph Platform REST API as understood by `langgraph-sdk ~0.1`. This means:

1. **Response shapes** — JSON response structures match what `langgraph-sdk` expects. Required fields, types, and nesting are tested via contract tests in `tests/test_sdk_contracts.py`.

2. **Route paths** — URL patterns match the SDK's expected endpoints (e.g., `/assistants/search`, `/threads/{thread_id}/runs/wait`).

3. **Error format** — Error responses use `{"detail": "..."}` format matching the SDK's error parsing.

4. **SSE event format** — Streaming responses use the same SSE event structure (metadata -> data -> end events).

## Breaking Changes Policy

- Compatibility breaks with the mirrored SDK version are treated as **planned release work**, not incidental fixes.
- Before breaking a contract, the change must be documented in the CHANGELOG with migration guidance.
- Contract tests in `tests/test_sdk_contracts.py` serve as the automated verification gate.

## Version Testing

CI tests include:
- Unit tests with mock graphs (`tests/test_platform_routes.py`)
- SDK integration tests with real `langgraph-sdk` client via `httpx.MockTransport` (`tests/test_sdk_compat.py`)
- Contract shape tests (`tests/test_sdk_contracts.py`)
