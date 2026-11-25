"""Offline upstream baselines and installed-adapter parity; no cloud or model calls."""

from __future__ import annotations

from importlib.metadata import version
import importlib.util
import json
from pathlib import Path
import platform
import sys
from types import ModuleType
from typing import Any

import azure.functions as func
from langchain_core.load import dumpd
from langgraph.checkpoint.memory import InMemorySaver

from azure_functions_langgraph import LangGraphApp

ROOT = Path(__file__).resolve().parents[1]
BASIC_SOURCE = """from langchain.messages import AnyMessage
from typing_extensions import TypedDict

class State(TypedDict):
    messages: list[AnyMessage]
    extra_field: int

from langchain.messages import AIMessage

def node(state: State):
    messages = state["messages"]
    new_message = AIMessage("Hello!")
    return {"messages": messages + [new_message], "extra_field": 10}

from langgraph.graph import StateGraph

builder = StateGraph(State)
builder.add_node(node)
builder.set_entry_point("node")
graph = builder.compile()
"""
CONVERSATION_SOURCE = """from langgraph.graph import StateGraph, MessagesState, START

def call_model(state: MessagesState):
    response = model.invoke(state["messages"])
    return {"messages": response}

builder = StateGraph(MessagesState)
builder.add_node(call_model)
builder.add_edge(START, "call_model")
"""


def load_file(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def message_contents(output: dict[str, Any]) -> list[str]:
    return [
        message.get("kwargs", message.get("data", message))["content"]
        for message in output["messages"]
    ]


def baseline(example: str) -> ModuleType:
    directory = ROOT / "examples" / example
    if example == "official_langgraph_basic":
        module = ModuleType("_pristine_basic")
        sys.modules[module.__name__] = module
        exec(BASIC_SOURCE, module.__dict__)
        return module
    model = load_file(directory / "model.py", "model").model
    module = ModuleType("_pristine_conversation")
    sys.modules[module.__name__] = module
    setattr(module, "model", model)
    exec(CONVERSATION_SOURCE, module.__dict__)
    setattr(module, "graph", getattr(module, "builder").compile(checkpointer=InMemorySaver()))
    return module


def run(example: str) -> dict[str, Any]:
    upstream = baseline(example)
    directory = ROOT / "examples" / example
    if example == "official_langgraph_basic":
        hosted = load_file(directory / "upstream_graph.py", "_hosted_basic").graph
        name = "official_basic"
        calls = [("a", "Hi")]
    else:
        load_file(directory / "model.py", "model")
        hosted = load_file(directory / "upstream_graph.py", "upstream_graph").builder.compile(
            checkpointer=InMemorySaver()
        )
        name = "official_conversation"
        calls = [("a", "Hi"), ("a", "Continue"), ("b", "Separate")]
    app = LangGraphApp()
    app.register(graph=hosted, name=name)
    handler = next(
        function.get_user_function()
        for function in app.function_app.get_functions()
        if function.get_function_name() == f"aflg_{name}_invoke"
    )
    results = []
    for thread, text in calls:
        payload = {
            "input": {"messages": [{"role": "human", "content": text}]},
            "config": {"configurable": {"thread_id": thread}},
        }
        expected = dumpd(upstream.graph.invoke(payload["input"], payload["config"]))
        response = handler(
            func.HttpRequest(
                method="POST",
                url="/invoke",
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
            )
        )
        assert response.status_code == 200
        output = json.loads(response.get_body())["output"]
        assert message_contents(output) == message_contents(expected)
        if example == "official_langgraph_basic":
            assert output["extra_field"] == expected["extra_field"] == 10
        results.append(message_contents(output))
    expected_contents = (
        [["Hi", "Hello!"]]
        if example == "official_langgraph_basic"
        else [
            ["Hi", "observed 1 human turn(s)"],
            ["Hi", "observed 1 human turn(s)", "Continue", "observed 2 human turn(s)"],
            ["Separate", "observed 1 human turn(s)"],
        ]
    )
    assert results == expected_contents
    return {
        "python": platform.python_version(),
        "versions": {
            distribution: version(distribution)
            for distribution in (
                "azure-functions-langgraph",
                "azure-functions",
                "langgraph",
                "langchain-core",
                "langgraph-checkpoint",
            )
        },
        "baseline_and_handler_contents": results,
        "scope": "offline baseline and handler",
    }


if __name__ == "__main__":
    import azure_functions_langgraph

    assert not Path(azure_functions_langgraph.__file__).resolve().is_relative_to(ROOT)
    print(json.dumps(run(sys.argv[1]), indent=2))
