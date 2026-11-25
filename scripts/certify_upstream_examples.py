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
BASELINES = Path(__file__).resolve().parent / "upstream_baselines"
CONVERSATION_SOURCE = (BASELINES / "conversation.py").read_text()


def load_file(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Python certification module: {path}")
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
        return load_file(BASELINES / "basic.py", "_pristine_basic")
    load_file(directory / "model.py", "model")
    module = load_file(BASELINES / "conversation.py", "_pristine_conversation")
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
        if response.status_code != 200:
            raise RuntimeError("registered handler invoke failed")
        output = json.loads(response.get_body())["output"]
        if message_contents(output) != message_contents(expected):
            raise RuntimeError("registered handler differs from upstream baseline")
        if example == "official_langgraph_basic":
            if not output["extra_field"] == expected["extra_field"] == 10:
                raise RuntimeError("registered handler differs from upstream extra_field")
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
    if results != expected_contents:
        raise RuntimeError("baseline and handler contents differ from expected conversation")
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


def require_installed_adapter(module_path: Path) -> None:
    if module_path.resolve().is_relative_to(ROOT):
        raise RuntimeError("certification requires an installed adapter, not the source tree")


if __name__ == "__main__":
    import azure_functions_langgraph

    require_installed_adapter(Path(azure_functions_langgraph.__file__))
    print(json.dumps(run(sys.argv[1]), indent=2))
