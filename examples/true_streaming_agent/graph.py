"""True-streaming agent example — LangGraph graph served with incremental SSE.

Unlike the other examples (which use ``LangGraphApp`` and buffered SSE), this
example uses :class:`azure_functions_langgraph.streaming.StreamingLangGraphApp`,
which serves ``POST /api/graphs/{name}/stream`` as **true** HTTP streaming: each
event the graph emits is flushed to the client as it is produced, instead of
being buffered until the run completes.

To make incremental delivery visible without a real LLM, the graph is a small
multi-step pipeline: each node appends one token, so ``graph.stream(...)`` yields
one state update per step. Over the wire you therefore see one ``event: data``
frame arrive per step (try ``curl -N`` — see the README).

Requirements::

    pip install "azure-functions-langgraph[streaming]" langgraph langchain-core

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
    tokens: list[str]


# ------------------------------------------------------------------
# 2. Define node functions — one appended token per step
# ------------------------------------------------------------------


def _reply_words(state: AgentState) -> list[str]:
    text = state["messages"][-1]["content"] if state["messages"] else ""
    return ["Echo:", *text.split()] if text else ["Echo:"]


def step_1(state: AgentState) -> dict[str, Any]:
    words = _reply_words(state)
    return {"tokens": words[:1]}


def step_2(state: AgentState) -> dict[str, Any]:
    words = _reply_words(state)
    return {"tokens": words[:2]}


def step_3(state: AgentState) -> dict[str, Any]:
    words = _reply_words(state)
    reply = " ".join(words)
    return {
        "tokens": words,
        "messages": state["messages"] + [{"role": "assistant", "content": reply}],
    }


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module can be imported without langgraph
# installed (e.g. for testing the example structure).
try:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(AgentState)
    builder.add_node("step_1", step_1)
    builder.add_node("step_2", step_2)
    builder.add_node("step_3", step_3)
    builder.add_edge(START, "step_1")
    builder.add_edge("step_1", "step_2")
    builder.add_edge("step_2", "step_3")
    builder.add_edge("step_3", END)

    compiled_graph = builder.compile()

except ImportError:
    raise ImportError(
        "langgraph is required for this example. "
        "Install it with: pip install langgraph langchain-core"
    )
