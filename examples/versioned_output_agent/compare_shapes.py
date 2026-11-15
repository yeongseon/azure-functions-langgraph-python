"""Print the v1 and v2 response shapes side by side, fully offline.

Drives the real ``/invoke`` and ``/stream`` handlers in-process: no Azure
Functions host, no network, no credentials, no model calls (the graph is a
deterministic counter).

Run from this directory::

    PYTHONPATH=../../src python compare_shapes.py
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

import azure.functions as func
from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp

GRAPH_NAME = "versioned_output_agent"


def build_handlers() -> dict[str, Callable[[func.HttpRequest], func.HttpResponse]]:
    """Register the graph and return its ``invoke`` / ``stream`` HTTP handlers by route."""
    app = LangGraphApp()
    app.register(graph=compiled_graph, name=GRAPH_NAME)
    # ``get_functions()`` validates names and must only be called once per app.
    by_name = {
        fn.get_function_name(): fn.get_user_function() for fn in app.function_app.get_functions()
    }
    return {route: by_name[f"aflg_{GRAPH_NAME}_{route}"] for route in ("invoke", "stream")}


def call(
    handlers: dict[str, Callable[[func.HttpRequest], func.HttpResponse]],
    route: str,
    body: dict[str, Any],
) -> func.HttpResponse:
    """POST ``body`` to the ``invoke`` or ``stream`` handler."""
    handler = handlers[route]
    request = func.HttpRequest(
        method="POST",
        url=f"/api/graphs/{GRAPH_NAME}/{route}",
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return handler(request)


def stream_data_events(response: func.HttpResponse) -> list[Any]:
    """Parse the buffered SSE body into the JSON payload of each ``data`` event."""
    events: list[Any] = []
    for block in response.get_body().decode().split("\n\n"):
        lines = block.strip().splitlines()
        if lines and lines[0] == "event: data":
            events.append(json.loads(lines[1].removeprefix("data: ")))
    return events


def main() -> None:
    handlers = build_handlers()
    payload = {"input": {"count": 1, "history": []}}

    v1 = json.loads(call(handlers, "invoke", payload).get_body())
    print("invoke v1:", json.dumps(v1))
    v2 = json.loads(call(handlers, "invoke", {**payload, "version": "v2"}).get_body())
    print("invoke v2:", json.dumps(v2))

    stream_payload = {**payload, "stream_mode": "values"}
    print("stream v1:", json.dumps(stream_data_events(call(handlers, "stream", stream_payload))))
    print(
        "stream v2:",
        json.dumps(
            stream_data_events(call(handlers, "stream", {**stream_payload, "version": "v2"}))
        ),
    )


if __name__ == "__main__":
    main()
