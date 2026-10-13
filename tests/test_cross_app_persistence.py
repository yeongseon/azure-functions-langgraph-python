from __future__ import annotations

import json
from typing import Any, Protocol, TypedDict
import uuid

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph import LangGraphApp


class SharedState(TypedDict):
    writer: str
    visits: int


class HttpHandler(Protocol):
    def __call__(self, request: func.HttpRequest) -> func.HttpResponse: ...


def _advance(state: SharedState) -> dict[str, Any]:
    return {"writer": state["writer"], "visits": state.get("visits", 0) + 1}


def _app(checkpointer: MemorySaver) -> LangGraphApp:
    builder = StateGraph(SharedState)
    builder.add_node("advance", _advance)
    builder.add_edge(START, "advance")
    builder.add_edge("advance", END)
    app = LangGraphApp()
    app.register(builder.compile(checkpointer=checkpointer), name="shared")
    return app


def _handler(app: LangGraphApp, name: str) -> HttpHandler:
    function_app = app.function_app
    function_app.functions_bindings = {}
    for function in function_app.get_functions():
        if function.get_function_name() == name:
            return function.get_user_function()
    raise AssertionError(f"Function {name!r} not found")


def _invoke(app: LangGraphApp, thread_id: str, writer: str) -> func.HttpResponse:
    request = func.HttpRequest(
        method="POST",
        url="http://test/api/graphs/shared/invoke",
        body=json.dumps(
            {
                "input": {"writer": writer},
                "config": {"configurable": {"thread_id": thread_id}},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    return _handler(app, "aflg_shared_invoke")(request)


def _state(app: LangGraphApp, thread_id: str) -> dict[str, Any]:
    request = func.HttpRequest(
        method="GET",
        url=f"http://test/api/graphs/shared/threads/{thread_id}/state",
        body=b"",
        route_params={"thread_id": thread_id},
    )
    response = _handler(app, "aflg_shared_state")(request)
    assert response.status_code == 200
    body: dict[str, dict[str, Any]] = json.loads(response.get_body())
    return body["values"]


def test_two_apps_share_checkpointed_thread_state() -> None:
    checkpointer = MemorySaver()
    app_a = _app(checkpointer)
    app_b = _app(checkpointer)
    thread_id = f"local-persistence-{uuid.uuid4().hex}"

    response_a = _invoke(app_a, thread_id, "app-a")
    assert response_a.status_code == 200
    assert _state(app_b, thread_id) == {"writer": "app-a", "visits": 1}

    response_b = _invoke(app_b, thread_id, "app-b")
    assert response_b.status_code == 200
    assert _state(app_a, thread_id) == {"writer": "app-b", "visits": 2}
