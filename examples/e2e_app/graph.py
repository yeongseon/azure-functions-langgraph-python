"""Two-node greeting graph used by the real-Azure e2e certification.

Purpose-built for `tests/e2e` — kept separate from the user-facing
`examples/simple_agent` so docs can evolve without breaking the release gate.
The graph performs NO LLM call: ``greet`` -> ``farewell``.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict):
    messages: list[dict[str, str]]
    greeting: str


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


from langgraph.graph import END, START, StateGraph  # noqa: E402

builder = StateGraph(AgentState)
builder.add_node("greet", greet)
builder.add_node("farewell", farewell)
builder.add_edge(START, "greet")
builder.add_edge("greet", "farewell")
builder.add_edge("farewell", END)

compiled_graph = builder.compile()
