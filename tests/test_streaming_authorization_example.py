"""Offline smoke test for examples/streaming_authorization (allowed vs denied stream)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# The example needs the optional ``streaming`` extra; skip cleanly when it is absent.
pytest.importorskip("azurefunctions.extensions.http.fastapi")
pytest.importorskip("starlette")

from tests.example_imports import load_example_file  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "streaming_authorization"
DENIED_BODY = {"error": "error", "detail": "Access denied", "request_id": "demo"}


@pytest.fixture
def demo() -> Any:
    module = load_example_file(EXAMPLE / "demo.py", "_examples_streaming_authorization_demo")
    module.EXECUTIONS.clear()
    return module


async def test_allowed_request_runs_graph_and_streams(demo: Any) -> None:
    handler = demo.stream_handler()
    status, body = await demo.drive(
        await handler(demo.make_request("demo-token-alice", "alice-thread", "hi"))
    )
    assert status == 200
    assert demo.EXECUTIONS == ["hi"]
    assert "event: data" in body and "echo: hi" in body
    assert body.rstrip().endswith("event: end\ndata: {}")


@pytest.mark.parametrize(
    ("token", "thread", "expected"),
    [
        (None, "alice-thread", 401),
        ("not-a-real-token", "alice-thread", 401),
        ("demo-token-bob", "alice-thread", 403),
        ("demo-token-alice", "bob-thread", 403),
        ("demo-token-alice", None, 403),
    ],
)
async def test_denied_request_never_starts_graph_or_leaks(
    demo: Any,
    token: str | None,
    thread: str | None,
    expected: int,
) -> None:
    handler = demo.stream_handler()
    status, body = await demo.drive(await handler(demo.make_request(token, thread)))
    assert status == expected
    assert demo.EXECUTIONS == []
    assert json.loads(body) == DENIED_BODY
    for leaked in ("internal-note-canary", "alice-thread", "demo-token", "echo:"):
        assert leaked not in body


def test_function_key_layer_is_independent_of_policy(demo: Any) -> None:
    import azure.functions as func

    app = demo.langgraph_app
    assert app.auth_level == func.AuthLevel.FUNCTION
    assert app.authorize is not None
