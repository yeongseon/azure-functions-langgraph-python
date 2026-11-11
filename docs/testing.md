# Testing

## Running tests

```bash
make test
```

This runs pytest with coverage reporting. The minimum coverage threshold is 95%.

## Example verification levels

The repository uses four distinct checks. Passing one does not imply the others:

| Level | What it proves | Entry point |
| --- | --- | --- |
| Import smoke | Example modules compile and register expected Python objects in the repository test environment. It does not resolve the example's standalone requirements. | `make test` (`tests/test_examples_smoke.py`) |
| Dependency resolution | A fresh Python 3.11 virtual environment can install each primary example's complete `requirements.txt`, pass `uv pip check`, import its fake-model route, and construct the real Azure OpenAI client under a mock without credentials or requests. | `make certify-examples-published` |
| Functions host smoke | The real Azure Functions Core Tools host starts and serves a native graph route. It uses the dedicated host-smoke app, not every primary example. | CI `host-smoke` job |
| Cloud certification | The release candidate is deployed to Azure and exercised over live HTTP. This is the release workflow's `azure-e2e` gate, not a pull-request dependency check. | `.github/workflows/publish-pypi.yml` |

The primary-example dependency lane has two modes:

```bash
# Newcomer path: install each checked-in requirements file unchanged from PyPI.
make certify-examples-published

# Development path: keep every example dependency, but replace only the adapter
# requirement with the supplied locally built wheel.
make build
make certify-examples-candidate WHEEL=dist/azure_functions_langgraph-*.whl
```

Both modes write resolver reports, installed versions, fake-route output, and
mocked real-model construction output under the system temporary directory by
default; CI supplies an explicit artifact path and uploads those files. A
deliberately incompatible fixture also has to fail resolution, proving the gate
detects conflicts rather than merely running commands. Neither mode invokes
Azure OpenAI or requires Azure credentials.

## Test levels

Choose the cheapest level that can actually prove the claim. A mocked test never certifies real Azure behavior, and each level below says what it does not prove.

| Level | Proves | Does not prove | Where / command |
| --- | --- | --- | --- |
| Mocked app contracts | HTTP status codes, request validation, response shapes, route registration and error handling of `LangGraphApp`, using the fake graphs described below | Real graph execution, real storage, Azure behavior | `tests/test_app.py`, `tests/test_streaming_*.py`, `tests/test_native_*.py` via `make test` |
| Deterministic storage tests | Checkpointer, thread-store and lock logic against in-memory fakes of the Azure SDK clients (for example `tests/test_checkpointers_azure_blob.py`, `tests/test_stores_azure_table.py`, `tests/test_locks_*.py`) | Real service semantics: latency, throttling, lease timing, eventual consistency | `make test` |
| Emulator / service-backed integration | The same stores against Cosmos DB or Azurite. Opt-in (`-m integration`); `COSMOS_REQUIRED` / `AZURITE_REQUIRED` make a missing service a failure instead of a skip | Real Azure Storage or Cosmos DB | `make test-cosmos`, `make test-table` (`tests/integration/`) |
| Functions host smoke | The real Azure Functions Core Tools host starts and serves a native graph route from `tests/host_smoke/` | Every example, or any cloud behavior | `make host-smoke` (CI `host-smoke` job) |
| Azure / Durable certification | The release candidate deployed to real Azure and exercised over live HTTP (`tests/e2e/`, marker `e2e`); the only level that certifies real Azure recovery | Anything not covered by the e2e routes | `make e2e-azure` and the `azure-e2e` gate in `.github/workflows/publish-pypi.yml` |

Negative-path tests (invalid JSON, a graph that raises, a missing graph state) use `FakeFailingGraph`, `FakeFailingStatefulGraph`, `FakeNotFoundStatefulGraph` and `FakeFailingUpdateStateGraph` at the mocked-contract level. Stateful tests (`get_state`, `update_state`, state history) use `FakeStatefulGraph`. These fakes return configured values; they do not execute a graph, so they cannot verify replay or checkpoint-resume semantics. Replay and recovery claims belong to the storage and e2e levels above.

The Azure Table benchmark (`tools/benchmark_table_store.py`) is a measurement tool, not a test level. Its `fake` backend models no network or service latency, and its `azurite` backend measures SDK plus emulator latency only. Neither is Azure Storage: do not quote those numbers as Azure measurements or derive a supported thread count from them. The optional real-Azure mode measures only the endpoint and network path where you run it.

## Test structure

```
tests/
├── conftest.py            # Shared fake graphs, fixtures and example-import isolation
├── example_imports.py     # Loads example files without leaking their sibling modules
├── test_app.py            # LangGraphApp core tests
├── test_contracts.py      # Pydantic model tests
├── test_protocols.py      # Protocol interface tests
├── test_public_api.py     # Public API surface tests
├── integration/           # Opt-in emulator / service-backed tests (-m integration)
├── host_smoke/            # Function app used by the Functions host smoke
└── e2e/                   # Real-Azure tests (-m e2e)
```

The listing is a map, not an inventory; browse `tests/` for the full set.

## Fake graph fixtures

Tests use fake graph implementations instead of real LangGraph graphs. They are defined in `tests/conftest.py` and exposed as the fixtures `fake_graph`, `fake_graph_with_checkpointer`, `fake_invoke_only_graph`, `fake_failing_graph`, `fake_stateful_graph`, `fake_failing_stateful_graph`, `fake_not_found_stateful_graph` and `fake_failing_update_state_graph`. The snippets below are checked against `tests/conftest.py` by `tests/test_testing_docs.py`.

Each fake returns canned values: it does not look at its input, so a passing test shows how the app handles a graph's result, not that a graph computed it.

### FakeCompiledGraph

Satisfies `LangGraphLike` (both `invoke()` and `stream()`). `invoke()` returns the configured `invoke_result` and `stream()` yields the configured `stream_results`, ignoring the input:

```python
class FakeCompiledGraph:
    def __init__(
        self,
        invoke_result: dict[str, Any] | None = None,
        stream_results: list[dict[str, Any]] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._invoke_result = invoke_result or {
            "messages": [{"role": "assistant", "content": "Hello!"}]
        }
        self._stream_results = stream_results or [
            {"messages": [{"role": "assistant", "content": "chunk1"}]},
            {"messages": [{"role": "assistant", "content": "chunk2"}]},
        ]
        self.checkpointer = checkpointer

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._invoke_result

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield from self._stream_results
```

### FakeInvokeOnlyGraph

Satisfies `InvocableGraph` only (no `stream()` method):

```python
class FakeInvokeOnlyGraph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": "ok"}
```

### FakeFailingGraph

Raises from both `invoke()` and `stream()`:

```python
class FakeFailingGraph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("Graph execution failed")

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        raise RuntimeError("Stream execution failed")
```

### Stateful fakes

`FakeStatefulGraph` adds `get_state()`, `update_state()` and `get_state_history()` on top of `FakeCompiledGraph`, backed by a configurable `_FakeStateSnapshot`. `FakeFailingStatefulGraph`, `FakeNotFoundStatefulGraph` and `FakeFailingUpdateStateGraph` cover the error paths of the state routes. Read `tests/conftest.py` for their exact behavior rather than relying on a copy here.

## Writing new tests

### Testing graph behavior

Use a fake fixture to test endpoint behavior (this mirrors `TestInvokeHandler` in `tests/test_app.py`):

```python
def test_my_feature(self, fake_graph: FakeCompiledGraph) -> None:
    app = LangGraphApp()
    app.register(graph=fake_graph, name="test")

    req = self._make_request({"input": {"messages": []}})
    resp = app._handle_invoke(req, app._registrations["test"])

    assert resp.status_code == 200
```

`_make_request` is a small helper defined on the test class that builds a `func.HttpRequest` with a JSON body.

### Testing error cases

```python
def test_invalid_json(self, fake_graph: FakeCompiledGraph) -> None:
    app = LangGraphApp()
    app.register(graph=fake_graph, name="test")

    req = func.HttpRequest(
        method="POST",
        url="/api/graphs/test/invoke",
        body=b"not json",
    )
    resp = app._handle_invoke(req, app._registrations["test"])
    assert resp.status_code == 400
```

### Importing examples safely

An example's `function_app.py` imports its siblings by bare name (`graph`, `model_config`, `tools`), so two examples loaded in one process would otherwise see each other's modules. Load example code through `tests.example_imports.load_example_file()`, which forgets those names first; the autouse fixture in `tests/conftest.py` restores `sys.modules` and `sys.path` after every test. Do not import an example with a plain `import graph` in a test.

## Coverage

Run with HTML coverage report:

```bash
make cov
```

View the report:

```bash
open htmlcov/index.html
```

The `coverage.xml` file is generated for CI integration with Codecov.
