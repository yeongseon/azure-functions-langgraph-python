"""Compiled graph for the managed_identity_storage example."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]
    turn: int


def respond(state: AgentState) -> dict[str, Any]:
    user_msg = state["messages"][-1]["content"] if state["messages"] else ""
    turn = state.get("turn", 0) + 1
    return {
        "messages": state["messages"]
        + [
            {
                "role": "assistant",
                "content": f"[turn {turn}] Echo: {user_msg}",
            }
        ],
        "turn": turn,
    }


def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder
