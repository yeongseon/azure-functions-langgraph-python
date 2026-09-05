"""Behavioural tests for the conversation_memory example.

These assert the *stateful delta* the example teaches, using the deterministic
fake-model fallback (no Azure OpenAI / Azure Storage credentials required):

* same ``thread_id`` across invocations accumulates conversation history,
* a different ``thread_id`` is isolated,
* the persisted thread state has the shape the native state endpoint returns.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "conversation_memory"


def _load_graph_module() -> Any:
    path = EXAMPLE_DIR / "graph.py"
    spec = importlib.util.spec_from_file_location("_conversation_memory_graph", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


@pytest.fixture()
def compiled_graph() -> Any:
    return _load_graph_module().compiled_graph


def _human_contents(messages: list[Any]) -> list[str]:
    return [m.content for m in messages if getattr(m, "type", None) == "human"]


def test_same_thread_accumulates_history(compiled_graph: Any) -> None:
    cfg = {"configurable": {"thread_id": "alice"}}

    first = compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "My name is Alice."}]}, cfg
    )
    assert _human_contents(first["messages"]) == ["My name is Alice."]

    second = compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "What is my name?"}]}, cfg
    )
    # The second invocation observes state written by the first: the earlier
    # human turn is replayed by the checkpointer before the graph runs.
    assert _human_contents(second["messages"]) == ["My name is Alice.", "What is my name?"]


def test_different_thread_is_isolated(compiled_graph: Any) -> None:
    compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "My name is Alice."}]},
        {"configurable": {"thread_id": "alice"}},
    )

    bob = compiled_graph.invoke(
        {"messages": [{"role": "human", "content": "What is my name?"}]},
        {"configurable": {"thread_id": "bob"}},
    )
    # Bob never sees Alice's turns — conversations are isolated per thread_id.
    assert _human_contents(bob["messages"]) == ["What is my name?"]


def test_state_snapshot_shape(compiled_graph: Any) -> None:
    cfg = {"configurable": {"thread_id": "carol"}}
    compiled_graph.invoke({"messages": [{"role": "human", "content": "hello"}]}, cfg)

    snapshot = compiled_graph.get_state(cfg)
    # Mirrors handle_state(): values is the graph state dict with messages.
    assert isinstance(snapshot.values, dict)
    assert "messages" in snapshot.values
    assert "hello" in _human_contents(snapshot.values["messages"])
    assert list(snapshot.next) == []
