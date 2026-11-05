"""Deterministic smoke test for ``examples/official_langgraph_basic``.

The example hosts a graph copied from the official LangGraph "Define and update
state" guide. This test keeps the "minimal changes" claim honest without any
network access or paid keys:

* the upstream snippets are pinned below and must appear verbatim in the hosted
  module (the graph logic is not rewritten);
* the pristine snippets are executed as the upstream BASELINE, and the hosted
  graph must produce the same result both natively and through the registered
  HTTP handler.

Issue: #777
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any

import azure.functions as func
from langchain.messages import HumanMessage

from azure_functions_langgraph.app import LangGraphApp

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "official_langgraph_basic"
GRAPH_NAME = "official_basic"

# Verbatim upstream snippets, langchain-ai/docs @ 42bc913ad89c7f288667184ae9fc485d9fa28023
# (MIT License, Copyright (c) 2025 LangChain), src/snippets/code-samples/
UPSTREAM_SNIPPETS = (
    "from langchain.messages import AnyMessage\n"
    "from typing_extensions import TypedDict\n"
    "\n"
    "\n"
    "class State(TypedDict):\n"
    "    messages: list[AnyMessage]\n"
    "    extra_field: int\n",
    "from langchain.messages import AIMessage\n"
    "\n"
    "\n"
    "def node(state: State):\n"
    '    messages = state["messages"]\n'
    '    new_message = AIMessage("Hello!")\n'
    '    return {"messages": messages + [new_message], "extra_field": 10}\n',
    "from langgraph.graph import StateGraph\n"
    "\n"
    "builder = StateGraph(State)\n"
    "builder.add_node(node)\n"
    'builder.set_entry_point("node")\n'
    "graph = builder.compile()\n",
)


def _pristine_upstream_graph() -> Any:
    """Execute the pinned upstream snippets exactly as published (the baseline)."""
    # A real module is needed: LangGraph resolves State's annotations via sys.modules.
    module = types.ModuleType("_official_langgraph_pristine_baseline")
    sys.modules[module.__name__] = module
    exec("\n".join(UPSTREAM_SNIPPETS), module.__dict__)  # noqa: S102 - pinned, trusted snippets
    return module.graph


def _load_hosted_module() -> Any:
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "_official_langgraph_basic_upstream_graph", EXAMPLE_DIR / "upstream_graph.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def _invoke_through_handler(payload: dict[str, Any]) -> dict[str, Any]:
    app = LangGraphApp()
    app.register(graph=_load_hosted_module().graph, name=GRAPH_NAME)
    app.function_app.functions_bindings = {}
    handlers = {
        fn.get_function_name(): fn.get_user_function() for fn in app.function_app.get_functions()
    }
    resp = handlers[f"aflg_{GRAPH_NAME}_invoke"](
        func.HttpRequest(
            method="POST",
            url=f"/api/graphs/{GRAPH_NAME}/invoke",
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            route_params={"name": GRAPH_NAME},
        )
    )
    assert resp.status_code == 200
    output: dict[str, Any] = json.loads(resp.get_body())["output"]
    return output


def _content(message: dict[str, Any]) -> Any:
    """Message text for either JSON message shape the adapter has emitted."""
    return message["data"]["content"] if "data" in message else message["content"]


def test_hosted_module_contains_upstream_snippets_verbatim() -> None:
    # Given: the hosted module and the pinned upstream snippets
    source = (EXAMPLE_DIR / "upstream_graph.py").read_text()

    # Then: every upstream snippet appears unchanged (no rewritten nodes/edges/state)
    for snippet in UPSTREAM_SNIPPETS:
        assert snippet in source


def test_hosted_graph_matches_upstream_baseline_natively() -> None:
    # Given: the upstream invocation from the guide, run on the pristine snippets
    request = {"messages": [HumanMessage("Hi")]}

    # When: the same request runs on the pristine and the hosted graph
    baseline = _pristine_upstream_graph().invoke(request)
    hosted = _load_hosted_module().graph.invoke(request)

    # Then: results are equal and match the guide's documented output
    assert hosted == baseline
    assert [m.content for m in baseline["messages"]] == ["Hi", "Hello!"]
    assert baseline["extra_field"] == 10


def test_registered_handler_matches_upstream_baseline_for_json_input() -> None:
    # Given: the JSON request the README documents (HTTP bodies cannot carry HumanMessage)
    payload = {"messages": [{"role": "human", "content": "Hi"}]}

    # When: the pristine graph and the registered handler receive the same input
    baseline = _pristine_upstream_graph().invoke(payload)
    output = _invoke_through_handler({"input": payload})

    # Then: meaningful result equals the baseline; no coercion is added by the host
    assert output["extra_field"] == baseline["extra_field"] == 10
    assert baseline["messages"][0] == {"role": "human", "content": "Hi"}
    assert output["messages"][0] == {"role": "human", "content": "Hi"}
    assert [_content(m) for m in output["messages"]] == ["Hi", baseline["messages"][1].content]
    assert _content(output["messages"][1]) == "Hello!"
