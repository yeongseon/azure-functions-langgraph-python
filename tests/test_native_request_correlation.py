from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
import json
import re
from types import SimpleNamespace
from typing import Any, Literal

import azure.functions as func
import pytest

from azure_functions_langgraph._handlers import (
    handle_invoke,
    handle_invoke_async,
    handle_stream,
    handle_stream_async,
)
from azure_functions_langgraph.observability import RunContext

Transport = Literal["sync_invoke", "async_invoke", "sync_stream", "async_stream"]


class GraphFailure(RuntimeError):
    pass


class CorrelationGraph:
    def __init__(self, *, fail: bool = False, checkpointed: bool = False) -> None:
        self.checkpointer = object() if checkpointed else None
        self._fail = fail

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._fail:
            raise GraphFailure
        return {"ok": True}

    async def ainvoke(
        self, input: dict[str, Any], config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._fail:
            raise GraphFailure
        return {"ok": True}

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield {"partial": True}
        if self._fail:
            raise GraphFailure

    async def astream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"partial": True}
        if self._fail:
            raise GraphFailure


class RecordingObserver:
    def __init__(self) -> None:
        self.contexts: list[RunContext] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.contexts.append(ctx)

    def on_run_completed(self, ctx: RunContext) -> None:
        self.contexts.append(ctx)

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.contexts.append(ctx)

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.contexts.append(ctx)


class ConfigurableLock:
    def __init__(self, *, contended: bool = False) -> None:
        self._contended = contended

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        return None if self._contended else "token"

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        return None


def _request(body: dict[str, Any], request_id: str | None) -> func.HttpRequest:
    headers = {"Content-Type": "application/json"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return func.HttpRequest(
        method="POST",
        url="https://example.test/api/graphs/agent/run",
        headers=headers,
        body=json.dumps(body).encode(),
    )


async def _run_handler(
    transport: Transport,
    *,
    graph: CorrelationGraph,
    observer: RecordingObserver,
    lock: ConfigurableLock,
    body: dict[str, Any],
    request_id: str | None = "caller-request_570",
) -> func.HttpResponse:
    req = _request(body, request_id)
    reg = SimpleNamespace(name="agent", graph=graph, stream_enabled=True)
    if transport == "sync_invoke":
        return handle_invoke(
            req,
            reg,
            thread_lock=lock,
            max_request_body_bytes=1_000_000,
            max_input_depth=32,
            max_input_nodes=10_000,
            observer=observer,
        )
    elif transport == "async_invoke":
        return await handle_invoke_async(
            req,
            reg,
            thread_lock=lock,
            max_request_body_bytes=1_000_000,
            max_input_depth=32,
            max_input_nodes=10_000,
            observer=observer,
        )
    elif transport == "sync_stream":
        return handle_stream(
            req,
            reg,
            thread_lock=lock,
            max_stream_response_bytes=1_000_000,
            max_request_body_bytes=1_000_000,
            max_input_depth=32,
            max_input_nodes=10_000,
            observer=observer,
        )
    return await handle_stream_async(
        req,
        reg,
        thread_lock=lock,
        max_stream_response_bytes=1_000_000,
        max_request_body_bytes=1_000_000,
        max_input_depth=32,
        max_input_nodes=10_000,
        observer=observer,
    )


@pytest.mark.parametrize(
    "transport",
    ["sync_invoke", "async_invoke", "sync_stream", "async_stream"],
)
@pytest.mark.parametrize(
    "scenario", ["success", "graph_error", "invalid_request", "invalid_config", "contention"]
)
async def test_native_handlers_use_one_request_id_across_response_and_observer(
    transport: Transport, scenario: str
) -> None:
    # Given
    observer = RecordingObserver()
    checkpointed = scenario == "contention"
    graph = CorrelationGraph(fail=scenario == "graph_error", checkpointed=checkpointed)
    lock = ConfigurableLock(contended=checkpointed)
    config: Any = {"configurable": "invalid"} if scenario == "invalid_config" else None
    if checkpointed:
        config = {"configurable": {"thread_id": "thread-570"}}
    input_value: Any = "invalid" if scenario == "invalid_request" else {}

    # When
    response = await _run_handler(
        transport,
        graph=graph,
        observer=observer,
        lock=lock,
        body={"input": input_value, "config": config},
    )

    # Then
    assert response.headers["X-Request-ID"] == "caller-request_570"
    assert observer.contexts
    assert {context.run_id for context in observer.contexts} == {"caller-request_570"}
    assert all(context.invocation_id is None for context in observer.contexts)
    if response.status_code >= 400:
        assert json.loads(response.get_body())["request_id"] == "caller-request_570"
    if "stream" in transport and scenario == "graph_error":
        frames = response.get_body().decode()
        assert frames.index("event: data") < frames.index("event: error")
        error_data = frames.split("event: error\ndata: ", maxsplit=1)[1].split("\n\n", maxsplit=1)[
            0
        ]
        assert json.loads(error_data)["request_id"] == "caller-request_570"


@pytest.mark.parametrize(
    ("request_id", "expected"),
    [
        (None, None),
        ("contains whitespace", None),
        ("x" * 65, None),
        ("safe.request-570", "safe.request-570"),
    ],
)
@pytest.mark.parametrize(
    "transport",
    ["sync_invoke", "async_invoke", "sync_stream", "async_stream"],
)
async def test_native_handlers_apply_safe_ingress_id_policy(
    transport: Transport, request_id: str | None, expected: str | None
) -> None:
    # Given
    observer = RecordingObserver()

    # When
    response = await _run_handler(
        transport,
        graph=CorrelationGraph(),
        observer=observer,
        lock=ConfigurableLock(),
        body={"input": {}},
        request_id=request_id,
    )

    # Then
    actual = response.headers["X-Request-ID"]
    assert actual == observer.contexts[0].run_id
    if expected is None:
        assert re.fullmatch(r"[0-9a-f]{32}", actual)
        assert actual != request_id
    else:
        assert actual == expected
