"""Offline demo: SQLite checkpoints survive closing and rebuilding the app.

Runs the example graph twice against one temporary SQLite file. Between the
two invocations the checkpointer's connection is closed and the graph and
``LangGraphApp`` are built again from scratch, so only the file can carry the
thread state forward. No Azure resources, credentials, or model API keys.

SQLite here is a local-development store; see README.md for its limits.

Run from this directory::

    python persistence_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any

from graph import build_graph

import azure.functions as func

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.sqlite import create_sqlite_checkpointer

THREAD_ID = "persistence-demo-thread"
GRAPH_NAME = "sqlite_agent"


def _invoke_request(text: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=f"/api/graphs/{GRAPH_NAME}/invoke",
        body=json.dumps(
            {
                "input": {"messages": [{"role": "human", "content": text}]},
                "config": {"configurable": {"thread_id": THREAD_ID}},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        route_params={"name": GRAPH_NAME},
    )


def _state_request() -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url=f"/api/graphs/{GRAPH_NAME}/threads/{THREAD_ID}/state",
        body=b"",
        route_params={"name": GRAPH_NAME, "thread_id": THREAD_ID},
    )


def _run_once(db_path: Path, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build everything fresh, invoke once, read the thread state, close the connection."""
    checkpointer = create_sqlite_checkpointer(str(db_path))
    try:
        app = LangGraphApp()
        app.register(graph=build_graph().compile(checkpointer=checkpointer), name=GRAPH_NAME)
        handlers = {
            fn.get_function_name(): fn.get_user_function()
            for fn in app.function_app.get_functions()
        }
        output = json.loads(handlers[f"aflg_{GRAPH_NAME}_invoke"](_invoke_request(text)).get_body())
        state = json.loads(handlers[f"aflg_{GRAPH_NAME}_state"](_state_request()).get_body())
        return output, state
    finally:
        checkpointer.conn.close()


def run_demo(db_path: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Two invocations on one thread, each with a freshly built app and checkpointer."""
    return [_run_once(db_path, "first"), _run_once(db_path, "second")]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "persistence-demo.sqlite"
        for label, (output, state) in zip(("first", "second"), run_demo(db_path), strict=True):
            print(f"{label} invocation output: {json.dumps(output, separators=(',', ':'))}")
            print(f"{label} invocation state : {json.dumps(state, separators=(',', ':'))}")


if __name__ == "__main__":
    main()
