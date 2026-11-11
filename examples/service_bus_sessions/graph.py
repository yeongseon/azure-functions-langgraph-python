"""Checkpointed counter graph used to make session/thread mapping visible.

Every run appends the incoming ``seen`` items to the checkpointed state and
increments ``turns``. Two runs that share a checkpoint ``thread_id`` therefore
show ``turns == 2``; runs on different threads each show ``turns == 1``.

Deterministic, no LLM, no network. ``MemorySaver`` is **in-process only**: use a
durable checkpointer (see ``production_persistent_agent``) for real deployments.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


class SessionState(TypedDict, total=False):
    seen: Annotated[list[str], operator.add]
    turns: int


def count_turn(state: SessionState) -> dict[str, Any]:
    return {"turns": state.get("turns", 0) + 1}


def build_graph() -> Any:
    """Return a fresh compiled graph with its own in-memory checkpointer."""
    builder = StateGraph(SessionState)
    builder.add_node("count_turn", count_turn)
    builder.add_edge(START, "count_turn")
    builder.add_edge("count_turn", END)
    return builder.compile(checkpointer=MemorySaver())


compiled_graph = build_graph()
