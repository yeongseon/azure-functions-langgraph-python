from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.checkpointers.sqlite import create_sqlite_checkpointer

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "sqlite_checkpoint_local"
THREAD_ID = "walkthrough-thread"


def _load_graph_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_sqlite_walkthrough_graph", EXAMPLE_DIR / "graph.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _post(text: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="/api/graphs/sqlite_agent/invoke",
        body=json.dumps(
            {
                "input": {"messages": [{"role": "human", "content": text}]},
                "config": {"configurable": {"thread_id": THREAD_ID}},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        route_params={"name": "sqlite_agent"},
    )


def test_sqlite_walkthrough_matches_live_handler_output(tmp_path: Path) -> None:
    # Given: the shipped turn-counting graph with its documented SQLite backend
    try:
        checkpointer = create_sqlite_checkpointer(str(tmp_path / "walkthrough.sqlite"))
    except ImportError as exc:
        pytest.skip(f"SQLite extra is unavailable or incompatible: {exc}")
    graph = _load_graph_module().build_graph().compile(checkpointer=checkpointer)
    app = LangGraphApp()
    app.register(graph=graph, name="sqlite_agent")
    handlers = {
        fn.get_function_name(): fn.get_user_function() for fn in app.function_app.get_functions()
    }

    # When: two invokes and a state read use one fixed thread
    invoke = handlers["aflg_sqlite_agent_invoke"]
    first = json.loads(invoke(_post("first")).get_body())
    second = json.loads(invoke(_post("second")).get_body())
    state = json.loads(
        handlers["aflg_sqlite_agent_state"](
            func.HttpRequest(
                method="GET",
                url=f"/api/graphs/sqlite_agent/threads/{THREAD_ID}/state",
                body=b"",
                route_params={"name": "sqlite_agent", "thread_id": THREAD_ID},
            )
        ).get_body()
    )
    transcript = (EXAMPLE_DIR / "README.md").read_text().split("## Walkthrough output", 1)[1]

    # Then: every documented stable response value comes from those handlers
    assert json.dumps(first, separators=(",", ":")) in transcript
    assert json.dumps(second, separators=(",", ":")) in transcript
    assert json.dumps(state, separators=(",", ":")) in transcript
    assert THREAD_ID in transcript
    assert "fresh database" in transcript
    assert "production_persistent_agent" in transcript
    assert "survive restarts" not in transcript
