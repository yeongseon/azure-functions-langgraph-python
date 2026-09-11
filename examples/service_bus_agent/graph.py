"""Service Bus agent example — LangGraph graph driven by a queue message.

This example shows a minimal LangGraph ``StateGraph`` that is invoked once per
Azure Service Bus **queue** message via ``LangGraphApp.register_service_bus``.

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core

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


class AgentState(TypedDict):
    messages: list[dict[str, str]]
    reply: str


# ------------------------------------------------------------------
# 2. Define node functions
# ------------------------------------------------------------------


def handle(state: AgentState) -> dict[str, Any]:
    """Single node — echoes the latest inbound message content."""
    text = state["messages"][-1]["content"] if state["messages"] else ""
    return {
        "reply": f"processed: {text}",
        "messages": state["messages"] + [{"role": "assistant", "content": f"processed: {text}"}],
    }


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module can be imported without langgraph
# installed (e.g. for testing the example structure).
try:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(AgentState)
    builder.add_node("handle", handle)
    builder.add_edge(START, "handle")
    builder.add_edge("handle", END)

    compiled_graph = builder.compile()

except ImportError:
    raise ImportError(
        "langgraph is required for this example. "
        "Install it with: pip install langgraph langchain-core"
    )
