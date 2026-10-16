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

## Test structure

```
tests/
├── conftest.py          # Shared fixtures
├── test_app.py          # LangGraphApp core tests
├── test_contracts.py    # Pydantic model tests
├── test_protocols.py    # Protocol interface tests
└── test_public_api.py   # Public API surface tests
```

## Fake graph fixtures

Tests use fake graph implementations instead of real LangGraph graphs. These are defined in `tests/conftest.py`:

### FakeCompiledGraph

A complete fake that satisfies `LangGraphLike` (both `invoke()` and `stream()`):

```python
class FakeCompiledGraph:
    def __init__(self, checkpointer: object | None = None) -> None:
        self.checkpointer = checkpointer

    def invoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"messages": input.get("messages", []) + [FAKE_REPLY]}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield {"messages": input.get("messages", [])}
        yield {"messages": input.get("messages", []) + [FAKE_REPLY]}
```

### FakeInvokeOnlyGraph

Satisfies `InvocableGraph` only (no `stream()` method):

```python
class FakeInvokeOnlyGraph:
    def invoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"messages": input.get("messages", []) + [FAKE_REPLY]}
```

### FakeFailingGraph

Always raises an exception:

```python
class FakeFailingGraph:
    def invoke(self, input: dict[str, Any], config: Any = None) -> Any:
        raise RuntimeError("Graph crashed")

    def stream(self, input: dict[str, Any], config: Any = None, stream_mode: str = "values") -> Any:
        raise RuntimeError("Stream crashed")
```

## Writing new tests

### Testing graph behavior

Use the fake fixtures to test endpoint behavior:

```python
def test_my_feature(self, fake_compiled_graph: FakeCompiledGraph) -> None:
    app = LangGraphApp()
    app.register(graph=fake_compiled_graph, name="test")

    req = self._make_request({"input": {"messages": []}})
    resp = app._handle_invoke(req, app._registrations["test"])

    assert resp.status_code == 200
```

### Testing error cases

```python
def test_invalid_json(self) -> None:
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
