from __future__ import annotations

import json
from typing import Any, TypedDict

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph import LangGraphApp


class _State(TypedDict):
    value: int


def _identity(state: _State) -> dict[str, Any]:
    return {"value": state["value"]}


def _request(endpoint: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=f"/api/graphs/agent/{endpoint}",
        body=json.dumps({"input": {"value": 1}}).encode(),
        headers={"Content-Type": "application/json"},
    )


def _checkpointed_app(*, async_mode: bool = False) -> LangGraphApp:
    builder = StateGraph(_State)
    builder.add_node("identity", _identity)
    builder.add_edge(START, "identity")
    builder.add_edge("identity", END)
    app = LangGraphApp()
    app.register(
        graph=builder.compile(checkpointer=MemorySaver()),
        name="agent",
        async_mode=async_mode,
    )
    return app


def test_invoke_rejects_missing_thread_id_for_checkpointed_graph() -> None:
    # Given
    app = _checkpointed_app()

    # When
    response = app._handle_invoke(_request("invoke"), app._registrations["agent"])

    # Then
    assert response.status_code == 400
    assert "thread_id" in json.loads(response.get_body())["detail"]


def test_stream_rejects_missing_thread_id_for_checkpointed_graph() -> None:
    # Given
    app = _checkpointed_app()

    # When
    response = app._handle_stream(_request("stream"), app._registrations["agent"])

    # Then
    assert response.status_code == 400
    assert "thread_id" in json.loads(response.get_body())["detail"]


async def test_async_invoke_rejects_missing_thread_id_for_checkpointed_graph() -> None:
    # Given
    app = _checkpointed_app(async_mode=True)

    # When
    response = await app._handle_invoke_async(_request("invoke"), app._registrations["agent"])

    # Then
    assert response.status_code == 400


async def test_async_stream_rejects_missing_thread_id_for_checkpointed_graph() -> None:
    # Given
    app = _checkpointed_app(async_mode=True)

    # When
    response = await app._handle_stream_async(_request("stream"), app._registrations["agent"])

    # Then
    assert response.status_code == 400
