"""Compiled graphs for the production_auth example."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class _State(TypedDict):
    messages: list[dict[str, str]]


def _respond(state: _State, *, prefix: str) -> dict[str, Any]:
    user_msg = state["messages"][-1]["content"] if state["messages"] else ""
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": f"{prefix}: {user_msg}"}]
    }


def _build(prefix: str) -> Any:
    builder = StateGraph(_State)
    builder.add_node("respond", lambda s: _respond(s, prefix=prefix))
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    return builder.compile()


private_graph = _build("private")
public_graph = _build("public")
