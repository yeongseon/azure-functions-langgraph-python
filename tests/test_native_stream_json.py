"""Native buffered SSE preserves graph event JSON types on both runtimes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
import json
from typing import Generic, TypeVar

import azure.functions as func
from langchain_core.messages import AIMessage, message_to_dict
from langgraph.graph import END, START, MessagesState, StateGraph
import pytest

from azure_functions_langgraph import LangGraphApp

T = TypeVar("T")
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class _CustomValue:
    def __str__(self) -> str:
        return "custom\nvalue"


@dataclass(frozen=True, slots=True)
class _EventGraph(Generic[T]):
    event: T

    def invoke(self, input: dict[str, str]) -> dict[str, str]:
        return input

    # Match the graph protocol's input/config/stream_mode calling convention.
    def stream(
        self, input: dict[str, str], config: dict[str, str], stream_mode: str
    ) -> Iterator[T]:
        assert stream_mode == "messages"
        yield self.event

    async def ainvoke(self, input: dict[str, str]) -> dict[str, str]:
        return input

    async def astream(
        self, input: dict[str, str], config: dict[str, str], stream_mode: str
    ) -> AsyncIterator[T]:
        assert stream_mode == "messages"
        yield self.event


def _request() -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="/api/graphs/agent/stream",
        body=b'{"input": {}, "stream_mode": "messages"}',
        headers={"Content-Type": "application/json", "X-Request-ID": "stream-json"},
    )


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            (AIMessage(content="hello"), {"langgraph_node": "chat"}),
            [message_to_dict(AIMessage(content="hello")), {"langgraph_node": "chat"}],
        ),
        (
            [{"nested": [AIMessage(content="nested")]}],
            [{"nested": [message_to_dict(AIMessage(content="nested"))]}],
        ),
        (
            {"messages": [AIMessage(content="dict")]},
            {"messages": [message_to_dict(AIMessage(content="dict"))]},
        ),
        ({"plain": [1, True, None]}, {"plain": [1, True, None]}),
        ("hello\nworld", "hello\nworld"),
        (42, 42),
        (1.5, 1.5),
        (True, True),
        (None, None),
        (_CustomValue(), "custom\nvalue"),
        ([_CustomValue()], ["custom\nvalue"]),
    ],
)
async def test_native_stream_preserves_json_values(
    async_mode: bool, event: T, expected: JsonValue
) -> None:
    # Given
    app = LangGraphApp()
    app.register(_EventGraph(event), "agent", async_mode=async_mode)

    # When: use the registered Azure Functions route, not the JSON helper alone.
    handler = next(
        fn.get_user_function()
        for fn in app.function_app.get_functions()
        if fn.get_function_name() == "aflg_agent_stream"
    )
    response = await handler(_request()) if async_mode else handler(_request())

    # Then: an SSE decoder sees the original JSON value and unchanged framing.
    frames = response.get_body().decode().strip().split("\n\n")
    assert len(frames) == 2
    assert frames[0].startswith("event: data\ndata: ")
    assert json.loads(frames[0].removeprefix("event: data\ndata: ")) == expected
    assert frames[1] == "event: end\ndata: {}"


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
async def test_compiled_graph_messages_retain_message_and_metadata(async_mode: bool) -> None:
    # Given: a real LangGraph messages-mode stream, without a model or network.
    def chat(state: MessagesState) -> MessagesState:
        return {"messages": [AIMessage(content="hello", id="reply")]}

    builder = StateGraph(MessagesState)
    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    app = LangGraphApp()
    app.register(builder.compile(), "agent", async_mode=async_mode)

    # When
    reg = app._registrations["agent"]
    response = (
        await app._handle_stream_async(_request(), reg)
        if async_mode
        else app._handle_stream(_request(), reg)
    )

    # Then
    frame = response.get_body().decode().split("\n\n")[0]
    message, metadata = json.loads(frame.removeprefix("event: data\ndata: "))
    assert message == message_to_dict(AIMessage(content="hello", id="reply"))
    assert metadata["langgraph_node"] == "chat"


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("event", [float("nan"), [float("inf")]])
async def test_native_stream_encoding_failure_keeps_error_and_end_frames(
    async_mode: bool, event: float | list[float]
) -> None:
    # Given
    app = LangGraphApp()
    app.register(_EventGraph(event), "agent", async_mode=async_mode)
    reg = app._registrations["agent"]

    # When
    response = (
        await app._handle_stream_async(_request(), reg)
        if async_mode
        else app._handle_stream(_request(), reg)
    )

    # Then
    error_frame, end_frame, trailing = response.get_body().decode().split("\n\n")
    assert json.loads(error_frame.removeprefix("event: error\ndata: ")) == {
        "error": "stream processing failed",
        "request_id": "stream-json",
    }
    assert end_frame == "event: end\ndata: {}"
    assert trailing == ""
