"""Native invoke encoding is part of the observed run, not post-run work (#696)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from typing import Protocol

import azure.functions as func
from langchain_core.messages import AIMessage, message_to_dict
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import NoOpRunObserver, RunContext


class Stringable(Protocol):
    def __str__(self) -> str: ...


@dataclass(frozen=True, slots=True)
class OutputGraph:
    value: Stringable
    calls: list[int] = field(default_factory=list)
    checkpointer: bool = True

    def invoke(
        self, input: dict[str, Stringable], config: dict[str, Stringable] | None = None
    ) -> dict[str, list[Stringable]]:
        self.calls.append(1)
        return {"nested": [self.value]}

    async def ainvoke(
        self, input: dict[str, Stringable], config: dict[str, Stringable] | None = None
    ) -> dict[str, list[Stringable]]:
        return self.invoke(input, config)


class Observer(NoOpRunObserver):
    def __init__(self) -> None:
        self.events: list[str] = []
        self.errors: list[BaseException] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, error: BaseException) -> None:
        self.events.append("failed")
        self.errors.append(error)


@dataclass(frozen=True, slots=True)
class EncodingProbe:
    events: list[str]

    def __str__(self) -> str:
        self.events.append("encoded")
        return "custom-output"


class BrokenString:
    def __str__(self) -> str:
        raise RuntimeError("secret-serialization-value")


async def invoke(app: LangGraphApp, async_mode: bool) -> func.HttpResponse:
    """Drive the app's registered native route with a correlated checkpointed request."""
    request = func.HttpRequest(
        method="POST",
        url="https://example.test/api/graphs/agent/invoke",
        headers={"X-Request-ID": "serialization-696"},
        body=b'{"input": {}, "config": {"configurable": {"thread_id": "thread-696"}}}',
    )
    for function in app.function_app.get_functions():
        if function.get_function_name() == "aflg_agent_invoke":
            handler = function.get_user_function()
            if async_mode:
                response: func.HttpResponse = await handler(request)
            else:
                response = handler(request)
            return response
    raise AssertionError("Native invoke route was not registered")


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (AIMessage(content="hello"), message_to_dict(AIMessage(content="hello"))),
        (datetime(2026, 1, 2, tzinfo=UTC), "2026-01-02 00:00:00+00:00"),
        ({1, 2}, "{1, 2}"),
        (b"\xff\xfe", "b'\\xff\\xfe'"),
        (
            {"message": AIMessage(content="nested")},
            {"message": message_to_dict(AIMessage(content="nested"))},
        ),
    ],
    ids=["message", "datetime", "set", "invalid-utf8", "nested-message"],
)
async def test_invoke_encodes_nested_values_when_shared_codec_supports_them(
    async_mode: bool, value: Stringable, expected: Stringable
) -> None:
    # Given a protocol-based graph returning nested Python values.
    graph = OutputGraph(value)
    observer = Observer()
    lock = InProcessThreadLock()
    app = LangGraphApp(observer=observer, thread_lock=lock)
    app.register(graph=graph, name="agent", async_mode=async_mode)

    # When the registered HTTP invoke route executes.
    response = await invoke(app, async_mode)

    # Then encoding preserves the envelope, correlation, and one terminal outcome.
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.headers["X-Request-ID"] == "serialization-696"
    assert json.loads(response.get_body()) == {"output": {"nested": [expected]}}
    assert observer.events == ["started", "completed"]
    assert graph.calls == [1]
    token = lock.acquire("agent", "thread-696")
    assert token is not None
    lock.release("agent", "thread-696", token)


@pytest.mark.parametrize("async_mode", [False, True])
async def test_invoke_encodes_unknown_object_before_reporting_completion(async_mode: bool) -> None:
    # Given a custom object whose encoding records the lifecycle ordering.
    observer = Observer()
    graph = OutputGraph(EncodingProbe(observer.events))
    app = LangGraphApp(observer=observer)
    app.register(graph=graph, name="agent", async_mode=async_mode)

    # When invoking the registered route.
    response = await invoke(app, async_mode)

    # Then the fallback runs exactly once, before completion.
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": {"nested": ["custom-output"]}}
    assert observer.events == ["started", "encoded", "completed"]
    assert graph.calls == [1]


@pytest.mark.parametrize("async_mode", [False, True])
async def test_invoke_encodes_plain_object_with_string_fallback(async_mode: bool) -> None:
    # Given an otherwise unknown object nested in graph output.
    value = object()
    graph = OutputGraph(value)
    app = LangGraphApp()
    app.register(graph=graph, name="agent", async_mode=async_mode)

    # When invoking the registered route.
    response = await invoke(app, async_mode)

    # Then the shared codec's string fallback is used rather than raising.
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": {"nested": [str(value)]}}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("value", [BrokenString(), float("nan"), {("bad",): "key"}])
async def test_invoke_reports_failure_when_encoding_fails(
    async_mode: bool, value: Stringable
) -> None:
    # Given output that cannot be encoded, with a real outer thread lock.
    graph = OutputGraph(value)
    observer = Observer()
    lock = InProcessThreadLock()
    app = LangGraphApp(observer=observer, thread_lock=lock)
    app.register(graph=graph, name="agent", async_mode=async_mode)

    # When invoking the registered route.
    response = await invoke(app, async_mode)

    # Then the client gets a sanitized error and cleanup releases the lock.
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "serialization-696"
    assert json.loads(response.get_body()) == {
        "error": "error",
        "detail": "Graph execution failed",
        "request_id": "serialization-696",
    }
    assert observer.events == ["started", "failed"]
    assert len(observer.errors) == 1
    assert graph.calls == [1]
    token = lock.acquire("agent", "thread-696")
    assert token is not None
    lock.release("agent", "thread-696", token)
