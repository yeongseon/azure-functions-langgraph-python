"""OTel run-observer example: a deterministic graph to exercise OTel tracing.

This example demonstrates the optional OpenTelemetry observer (issue #434),
which layers on top of the run-lifecycle observability contract (#425/#407).
The graph itself is deliberately deterministic — no LLM calls — so the example
runs in CI without any cloud credentials. The interesting part lives in
``function_app.py``, which wires an :class:`OTelRunObserver` onto the app.

Requirements::

    pip install "azure-functions-langgraph[otel]" "langgraph>=1.1"

Usage::

    # In your function_app.py
    from graph import compiled_graph
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class ChatState(TypedDict, total=False):
    user_text: str
    history: list[str]
    turn_count: int
    last_reply: str


def greet(state: ChatState) -> dict[str, Any]:
    """First node — build a greeting."""
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [*state.get("history", []), reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    """Second node — increment the turn counter."""
    return {"turn_count": (state.get("turn_count") or 0) + 1}


# NOTE: guarded so the module can be imported without langgraph installed
# (e.g. for testing the example structure).
try:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ChatState)
    builder.add_node("greet", greet)
    builder.add_node("count", count)
    builder.add_edge(START, "greet")
    builder.add_edge("greet", "count")
    builder.add_edge("count", END)

    compiled_graph = builder.compile()

except ImportError:
    raise ImportError(
        "langgraph is required for this example. "
        "Install it with: pip install 'langgraph>=1.1' langchain-core"
    )
