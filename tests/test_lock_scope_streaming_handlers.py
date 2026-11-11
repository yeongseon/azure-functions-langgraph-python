"""Generated StreamingLangGraphApp handlers honour the storage lock scope (#858).

Needs the optional ``streaming`` extra (FastAPI extension), so this module is
listed in ``tools/minimum_dependency_optional_tests.txt``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from azure_functions_langgraph.locks import InProcessThreadLock

THREAD_ID = "thread-1"


class StorageSaver:
    def __init__(self, identity: str) -> None:
        self.lock_scope_identity = identity


# --- #867 review (5): generated StreamingLangGraphApp handlers ----------------


class _CountingGraph:
    def __init__(self, identity: str) -> None:
        self.checkpointer = StorageSaver(identity)
        self.calls = 0

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> Any:
        self.calls += 1
        return input

    def stream(
        self, input: dict[str, Any], config: dict[str, Any] | None = None, stream_mode: str = "v"
    ) -> Any:
        self.calls += 1
        yield input


class _FakeRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self._raw = json.dumps(
            {"input": {"v": 1}, "config": {"configurable": {"thread_id": THREAD_ID}}}
        ).encode()

    async def body(self) -> bytes:
        return self._raw


async def _http_status(response: Any) -> int:
    """Final HTTP status; a deferred stream decides it when ASGI executes it."""
    if not hasattr(response, "_prepare"):
        return int(response.status_code)
    messages: list[Any] = []

    async def send(message: Any) -> None:
        messages.append(message)

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return int(messages[0]["status"])


def _streaming_handler(app: Any, fn_name: str) -> Any:
    fa = app.function_app
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(fn_name)


@pytest.mark.parametrize("fn_name", ["aflg_beta_invoke", "aflg_beta_stream"])
async def test_streaming_generated_handlers_contend_with_a_same_storage_graph(
    fn_name: str,
) -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    app = StreamingLangGraphApp(thread_lock=InProcessThreadLock())
    alpha, beta = _CountingGraph("s1"), _CountingGraph("s1")
    app.register(graph=alpha, name="alpha")
    app.register(graph=beta, name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("alpha", THREAD_ID) is not None  # held through the other graph
    response = await _streaming_handler(app, fn_name)(_FakeRequest())
    assert await _http_status(response) == 409
    assert (alpha.calls, beta.calls) == (0, 0)


@pytest.mark.parametrize("fn_name", ["aflg_beta_invoke", "aflg_beta_stream"])
async def test_streaming_generated_handlers_run_with_different_storage(fn_name: str) -> None:
    from azure_functions_langgraph.streaming import StreamingLangGraphApp

    app = StreamingLangGraphApp(thread_lock=InProcessThreadLock())
    app.register(graph=_CountingGraph("s1"), name="alpha")
    beta = _CountingGraph("s2")
    app.register(graph=beta, name="beta")
    scoped = app._scoped_thread_lock()
    assert scoped is not None
    assert scoped.acquire("alpha", THREAD_ID) is not None
    response = await _streaming_handler(app, fn_name)(_FakeRequest())
    assert await _http_status(response) == 200
    assert beta.calls == 1
