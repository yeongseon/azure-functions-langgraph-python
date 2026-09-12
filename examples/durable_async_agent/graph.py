"""Compiled graph for the durable async-agent example.

A deterministic two-node graph with a small, artificial "slow" step so the
durable async run lifecycle (create → poll pending/running → result) is
observable end to end without any LLM or external dependency. No clock or
randomness lives in the graph beyond a fixed sleep, and the whole thing runs in
the Durable *activity*, never the orchestrator.
"""

from __future__ import annotations

import time
from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]


def _slow_work(state: AgentState) -> dict[str, Any]:
    # A tiny, deterministic delay standing in for real work (LLM/tool calls).
    time.sleep(0.1)
    return state


def _respond(state: AgentState) -> dict[str, Any]:
    user_msg = state["messages"][-1]["content"]
    reply = {"role": "assistant", "content": f"Processed: {user_msg}"}
    return {"messages": state["messages"] + [reply]}


builder = StateGraph(AgentState)
builder.add_node("work", _slow_work)
builder.add_node("respond", _respond)
builder.add_edge(START, "work")
builder.add_edge("work", "respond")
builder.add_edge("respond", END)

compiled_graph = builder.compile()
