"""Compiled graph for the openapi_bridge example."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def echo(state: AgentState) -> dict[str, Any]:
    user_msg = state["messages"][-1]["content"] if state["messages"] else ""
    return {"messages": state["messages"] + [{"role": "assistant", "content": f"Echo: {user_msg}"}]}


_builder = StateGraph(AgentState)
_builder.add_node("echo", echo)
_builder.add_edge(START, "echo")
_builder.add_edge("echo", END)

compiled_graph = _builder.compile()
