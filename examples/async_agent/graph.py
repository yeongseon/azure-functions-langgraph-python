"""Async agent example: serve a graph through the async invoke/stream path.

This example demonstrates the **native async execution surface** (issue #422).
The graph's nodes are ``async def`` coroutines, and the app registers the graph
with ``async_mode=True`` so the native ``invoke``/``stream`` endpoints ``await``
``graph.ainvoke`` / ``graph.astream`` instead of the synchronous methods.

The node logic is deliberately deterministic — no LLM calls — so the example
runs in CI without any cloud credentials. ``await asyncio.sleep(0)`` stands in
for real awaitable I/O (an async HTTP client, an async DB driver, etc.).

* ``async_mode=True`` → the endpoints route through the async handlers, which
  ``await`` the graph and offload the per-thread lock to a worker thread so a
  blocking distributed lock backend never stalls the event loop.
* A ``CompiledStateGraph`` exposes **both** sync and async methods, so the same
  graph still works with the default (sync) path; the flag simply selects which
  method the handlers call.

Requirements::

    pip install azure-functions-langgraph "langgraph>=1.1"

Usage::

    # In your function_app.py
    from graph import compiled_graph
"""

from __future__ import annotations

import asyncio
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
# 2. Define async node functions
# ------------------------------------------------------------------


async def greet(state: ChatState) -> dict[str, Any]:
    """First node — build a greeting after awaiting simulated async I/O."""
    await asyncio.sleep(0)  # stand-in for a real ``await`` (HTTP, DB, ...)
    text = state.get("user_text", "")
    reply = f"Hello, {text}!" if text else "Hello!"
    return {"history": [*state.get("history", []), reply], "last_reply": reply}


async def count(state: ChatState) -> dict[str, Any]:
    """Second node — increment the turn counter."""
    await asyncio.sleep(0)
    return {"turn_count": (state.get("turn_count") or 0) + 1}


# ------------------------------------------------------------------
# 3. Build the graph (using LangGraph API)
# ------------------------------------------------------------------

# NOTE: This block is guarded so the module can be imported without langgraph
# installed (e.g. for testing the example structure).
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
