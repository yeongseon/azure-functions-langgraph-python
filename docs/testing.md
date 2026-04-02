# Testing

## Running tests

```bash
make test
```

This runs pytest with coverage reporting. The minimum coverage threshold is 80%.

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
