"""Deterministic notebook graph for the platform_thread_isolation example.

No model, no network: each run appends one note to the thread's ``notes`` list
(a reducer, so notes accumulate across runs) and bumps ``turns``. The
checkpointer is in-memory: it survives between runs inside ONE process only.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph


class NotebookState(TypedDict, total=False):
    note: str
    notes: Annotated[list[str], operator.add]
    turns: int


def remember(state: NotebookState) -> dict[str, Any]:
    return {"notes": [state.get("note", "")], "turns": (state.get("turns") or 0) + 1}


_builder = StateGraph(NotebookState)
_builder.add_node("remember", remember)
_builder.add_edge(START, "remember")
_builder.add_edge("remember", END)

compiled_graph = _builder.compile(checkpointer=MemorySaver())
