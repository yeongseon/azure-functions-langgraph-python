"""Behavioural tests for the tool_calling_agent example.

These assert the *tool-calling delta* the example teaches, using the
deterministic scripted fake-model fallback (no Azure OpenAI credentials
required):

* the expected tool executes exactly once with the expected arguments,
* the tool's result reaches the final graph result.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "tool_calling_agent"


def _load_graph_module() -> Any:
    path = EXAMPLE_DIR / "graph.py"
    spec = importlib.util.spec_from_file_location("_tool_calling_agent_graph", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


@pytest.fixture()
def compiled_graph() -> Any:
    return _load_graph_module().compiled_graph


def _tool_messages(messages: list[Any]) -> list[Any]:
    return [m for m in messages if getattr(m, "type", None) == "tool"]


def _final_ai_content(messages: list[Any]) -> str:
    ai = [m for m in messages if getattr(m, "type", None) == "ai"]
    assert ai, "expected at least one AIMessage in the result"
    return str(ai[-1].content)


def test_tool_executes_exactly_once_with_expected_args(compiled_graph: Any) -> None:
    result = compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "What is the status of order A1001?"}]}
    )

    tool_msgs = _tool_messages(result["messages"])
    # The scripted fake emits exactly one lookup_order call, so the ToolNode
    # runs the tool exactly once.
    assert len(tool_msgs) == 1
    # The tool ran with order_id="A1001" — its output proves both the tool and
    # the argument.
    assert tool_msgs[0].content == "Order A1001: shipped via Contoso Express, ETA 2 days."


def test_tool_result_reaches_final_answer(compiled_graph: Any) -> None:
    result = compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "What is the status of order A1001?"}]}
    )

    # The loop closes with a final AIMessage produced after the tool result was
    # handed back to the agent.
    final = _final_ai_content(result["messages"])
    assert final
    assert isinstance(final, str)
    # The scripted final answer references the order handled by the tool.
    assert "A1001" in final
