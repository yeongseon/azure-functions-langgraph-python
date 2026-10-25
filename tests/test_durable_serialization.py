from __future__ import annotations

import json
from typing import TypeAlias

import azure.functions as func
from langchain_core.messages import AIMessage, message_to_dict
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
import pytest

from azure_functions_langgraph.durable._runtime import (
    DurableRuntimeDeps,
    create_durable_function_app,
    get_run_http,
    register_durable_runtime,
)
from azure_functions_langgraph.durable._state import DurableRunPayload, normalize_status
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.test_durable import FakeDurableClient, FakeReg, FakeStatus, RecordingObserver, _req

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@pytest.mark.parametrize("async_mode", [False, True])
async def test_registered_activity_serializes_checkpointed_messages(async_mode: bool) -> None:
    # Given a real MessagesState graph whose checkpoint contains an AIMessage.
    message = AIMessage(content="hello", id="answer", additional_kwargs={"nested": [[1, 2]]})
    calls: list[str] = []

    def respond(state: MessagesState) -> MessagesState:
        calls.append("invoked")
        return {"messages": [message]}

    builder = StateGraph(MessagesState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    graph = builder.compile(checkpointer=MemorySaver())
    observer = RecordingObserver()
    app = create_durable_function_app(func.AuthLevel.FUNCTION)
    register_durable_runtime(
        app,
        DurableRuntimeDeps(
            registry={"chat": FakeReg(graph, "chat", async_mode=async_mode)},
            thread_lock=InProcessThreadLock(),
            observer=observer,
        ),
    )
    activity = next(
        f for f in app.get_functions() if f.get_function_name() == "execute_langgraph_run"
    )

    # When the actual registered activity returns its output for persistence.
    output = await activity.get_user_function()(
        DurableRunPayload("r1", "chat", {"messages": []}, thread_id="t1").to_dict()
    )

    # Then plain JSON preserves structured messages and the run completes once.
    persisted = json.loads(json.dumps(output, allow_nan=False))
    assert persisted["result"] == {"messages": [message_to_dict(message)]}
    assert persisted["activity_status"] == "success"
    assert normalize_status("Completed", persisted) == "success"
    assert observer.events == ["started", "completed"]
    assert calls == ["invoked"]
    assert graph.get_state({"configurable": {"thread_id": "t1"}}).values["messages"] == [message]


async def test_registered_activity_serializes_messages_in_nested_lists() -> None:
    # Given a graph returning messages below multiple list/dict layers.
    message = AIMessage(content="nested", id="nested-answer")
    graph: RunnableLambda[str, dict[str, list[list[dict[str, list[AIMessage]]]]]] = RunnableLambda(
        lambda _: {"nested": [[{"messages": [message]}]]}
    )
    app = create_durable_function_app(func.AuthLevel.FUNCTION)
    register_durable_runtime(
        app,
        DurableRuntimeDeps(
            registry={"chat": FakeReg(graph, "chat")}, thread_lock=InProcessThreadLock()
        ),
    )
    activity = next(
        f for f in app.get_functions() if f.get_function_name() == "execute_langgraph_run"
    )

    # When the activity crosses the JSON-only Durable boundary.
    output = await activity.get_user_function()(DurableRunPayload("r1", "chat", "input").to_dict())

    # Then no Python repr substitutes for a message's structured data.
    assert json.loads(json.dumps(output))["result"] == {
        "nested": [[{"messages": [message_to_dict(message)]}]]
    }


@pytest.mark.parametrize("failure", ["key", "cycle", "nan", "string"])
async def test_unserializable_output_is_terminal_and_safe_to_poll(failure: str) -> None:
    # Given output that cannot be encoded, potentially containing a secret.
    secret = "serialization-secret-canary"

    class BrokenString:
        def __str__(self) -> str:
            raise RuntimeError(secret)

    cycle: list[JsonValue] = []
    cycle.append(cycle)
    outputs: dict[str, JsonValue | dict[tuple[str], str] | BrokenString] = {
        "key": {(secret,): "unsupported key"},
        "cycle": cycle,
        "nan": float("nan"),
        "string": BrokenString(),
    }
    calls: list[str] = []

    def respond(_: str) -> JsonValue | dict[tuple[str], str] | BrokenString:
        calls.append("invoked")
        return outputs[failure]

    observer = RecordingObserver()
    lock = InProcessThreadLock()
    app = create_durable_function_app(func.AuthLevel.FUNCTION)
    register_durable_runtime(
        app,
        DurableRuntimeDeps(
            registry={"chat": FakeReg(RunnableLambda(respond), "chat")},
            thread_lock=lock,
            observer=observer,
        ),
    )
    activity = next(
        f for f in app.get_functions() if f.get_function_name() == "execute_langgraph_run"
    )

    # When the registered activity executes once and its output is polled.
    output = await activity.get_user_function()(DurableRunPayload("r1", "chat", "input").to_dict())
    persisted = json.loads(json.dumps(output, allow_nan=False))
    response = await get_run_http(
        FakeDurableClient(FakeStatus("Completed", persisted)),
        _req(route_params={"run_id": "r1"}, method="GET"),
    )

    # Then encoding failure is a normalized error, not a retry or false completion.
    assert persisted["activity_status"] == "error"
    assert persisted["result"] is None
    assert normalize_status("Completed", persisted) == "error"
    assert observer.events == ["started", "failed"]
    assert calls == ["invoked"]
    assert response.status_code == 200
    body = response.get_body().decode()
    assert json.loads(body)["status"] == "error"
    assert secret not in body
    assert "Traceback" not in body


async def test_repeated_activity_delivery_still_invokes_graph_each_time() -> None:
    # Given two deliveries with the same run ID, not an exactly-once adapter.
    calls: list[str] = []

    def respond(value: str) -> str:
        calls.append(value)
        return value

    app = create_durable_function_app(func.AuthLevel.FUNCTION)
    register_durable_runtime(
        app,
        DurableRuntimeDeps(
            registry={"chat": FakeReg(RunnableLambda(respond), "chat")},
            thread_lock=InProcessThreadLock(),
        ),
    )
    activity = next(
        f for f in app.get_functions() if f.get_function_name() == "execute_langgraph_run"
    )
    payload = DurableRunPayload("r1", "chat", "hello").to_dict()

    # When Durable delivers the same activity twice.
    outputs = [await activity.get_user_function()(payload) for _ in range(2)]

    # Then serialization adds no deduplication or internal invocation retry.
    assert calls == ["hello", "hello"]
    assert [json.loads(json.dumps(output))["result"] for output in outputs] == ["hello", "hello"]
