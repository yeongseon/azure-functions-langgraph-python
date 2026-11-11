"""Deterministic fake graph that records every execution (no LLM, no network)."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    message: str
    reply: str
    internal_note: str


# Every time the node body runs it appends here, so a test (or demo.py) can prove
# whether a request reached graph execution.
EXECUTIONS: list[str] = []


def respond(state: State) -> State:
    EXECUTIONS.append(state["message"])
    return {**state, "reply": "echo: " + state["message"], "internal_note": "internal-note-canary"}


_builder = StateGraph(State)
_builder.add_node("respond", respond)
_builder.add_edge(START, "respond")
_builder.add_edge("respond", END)

compiled_graph = _builder.compile()
