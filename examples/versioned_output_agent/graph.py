"""Versioned-output example: opt into LangGraph's ``version="v2"`` shapes.

This example shows the **request-level ``version`` pass-through** (issue #423).
The graph itself is deliberately trivial — a two-step counter — so the focus is
on how the native ``invoke``/``stream`` endpoints forward an optional
``version`` field through to ``graph.invoke(..., version=...)`` /
``graph.stream(..., version=...)``.

* ``version`` omitted (default) → legacy LangGraph output (a bare state dict for
  ``invoke``; ``stream_mode``-shaped events for ``stream``).
* ``version="v2"`` → the unified LangGraph 1.1+ shapes: ``invoke`` returns a
  ``GraphOutput`` serialized as ``{"value": ..., "interrupts": [...]}`` and
  ``stream`` emits ``StreamPart`` events ``{"type", "ns", "data", "interrupts"}``.

Requires ``langgraph>=1.1`` for ``version="v2"``; the default path works on any
supported LangGraph version.

Requirements::

    pip install azure-functions-langgraph "langgraph>=1.1"

Usage::

    # In your function_app.py
    from graph import compiled_graph
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict

# ------------------------------------------------------------------
# 1. Define state
# ------------------------------------------------------------------


class CounterState(TypedDict):
    count: int
    history: list[int]


# ------------------------------------------------------------------
# 2. Define node functions
# ------------------------------------------------------------------


def increment(state: CounterState) -> dict[str, Any]:
    """First node — bump the counter by one."""
    new_count = state["count"] + 1
    return {"count": new_count, "history": [*state.get("history", []), new_count]}


def double(state: CounterState) -> dict[str, Any]:
    """Second node — double the counter."""
    new_count = state["count"] * 2
    return {"count": new_count, "history": [*state.get("history", []), new_count]}


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module can be imported without langgraph
# installed (e.g. for testing the example structure).
try:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(CounterState)
    builder.add_node("increment", increment)
    builder.add_node("double", double)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "double")
    builder.add_edge("double", END)

    compiled_graph = builder.compile()

except ImportError:
    raise ImportError(
        "langgraph is required for this example. "
        "Install it with: pip install 'langgraph>=1.1' langchain-core"
    )
