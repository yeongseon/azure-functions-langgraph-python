"""Simple example: deploy a LangGraph agent as Azure Functions.

This example shows a minimal LangGraph StateGraph deployed via LangGraphApp.
The graph has two nodes: ``greet`` and ``farewell``, connected sequentially.

Requirements::

    pip install azure-functions-langgraph langgraph langchain-core

Usage::

    # In your function_app.py
    from examples.simple_agent.graph import func_app as app
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict

from azure_functions_langgraph import LangGraphApp

# ------------------------------------------------------------------
# 1. Define state
# ------------------------------------------------------------------


class AgentState(TypedDict):
    messages: list[dict[str, str]]
    greeting: str


# ------------------------------------------------------------------
# 2. Define node functions
# ------------------------------------------------------------------


def greet(state: AgentState) -> dict[str, Any]:
    """First node — generates a greeting."""
    user_msg = state["messages"][-1]["content"] if state["messages"] else "stranger"
    return {"greeting": f"Hello, {user_msg}!"}


def farewell(state: AgentState) -> dict[str, Any]:
    """Second node — appends farewell to messages."""
    return {
        "messages": state["messages"]
        + [{"role": "assistant", "content": f"{state['greeting']} Goodbye!"}]
    }


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module can be imported without langgraph
# installed (e.g. for testing the example structure).
try:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(AgentState)
    builder.add_node("greet", greet)
    builder.add_node("farewell", farewell)
    builder.add_edge(START, "greet")
    builder.add_edge("greet", "farewell")
    builder.add_edge("farewell", END)

    compiled_graph = builder.compile()

    # ------------------------------------------------------------------
    # 4. Deploy via LangGraphApp
    # ------------------------------------------------------------------

    langgraph_app = LangGraphApp()
    langgraph_app.register(
        graph=compiled_graph,
        name="simple_agent",
        description="A simple two-node greeting agent",
    )

    func_app = langgraph_app.function_app

except ImportError:
    # langgraph not installed — skip graph creation
    compiled_graph = None  # type: ignore[assignment]
    func_app = None  # type: ignore[assignment]
