"""App Insights observability example: a deterministic graph.

This example demonstrates shipping run telemetry to **Azure Application
Insights** using the package's built-in, dependency-free
:class:`~azure_functions_langgraph.observability.LoggingRunObserver` (issue
#407). The graph itself is deliberately deterministic — no LLM calls — so the
example runs in CI without any cloud credentials. The interesting part lives in
``function_app.py``, which wires the built-in observer, and in ``README.md``,
which shows the KQL queries that turn the emitted logs into dashboards.

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


class ChatState(TypedDict, total=False):
    user_text: str
    history: list[str]
    turn_count: int
    last_reply: str


# ------------------------------------------------------------------
# 2. Define node functions
# ------------------------------------------------------------------


def greet(state: ChatState) -> dict[str, Any]:
    """First node — build a greeting."""
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [*state.get("history", []), reply], "last_reply": reply}


def count(state: ChatState) -> dict[str, Any]:
    """Second node — increment the turn counter."""
    return {"turn_count": (state.get("turn_count") or 0) + 1}


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module surfaces a clear error if langgraph
# is not installed.
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
