"""Evidence for keeping per-request threadless graph cloning (#861)."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph.platform._common import _get_threadless_graph


class _State(TypedDict):
    value: str


def _compiled(*, checkpointed: bool) -> Any:
    builder = StateGraph(_State)
    builder.add_node("n", lambda s: {"value": "x"})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile(checkpointer=InMemorySaver() if checkpointed else None)


def test_clone_is_fresh_per_call_and_leaves_source_untouched() -> None:
    graph = _compiled(checkpointed=True)
    saver = graph.checkpointer
    first = _get_threadless_graph(graph)
    second = _get_threadless_graph(graph)
    assert first is not None and second is not None
    assert first is not graph and second is not graph and first is not second
    assert first.checkpointer is None and second.checkpointer is None
    assert graph.checkpointer is saver


def test_clone_observes_later_source_mutation() -> None:
    # A per-registration cache would freeze the registration-time graph and miss
    # this; that is why the clone is intentionally rebuilt per request.
    graph = _compiled(checkpointed=True)
    before = _get_threadless_graph(graph)
    graph.name = "renamed-after-registration"
    after = _get_threadless_graph(graph)
    assert before is not None and after is not None
    assert before.name != "renamed-after-registration"
    assert after.name == "renamed-after-registration"


def test_checkpointless_graph_is_returned_as_is() -> None:
    graph = _compiled(checkpointed=False)
    assert _get_threadless_graph(graph) is graph


def test_graph_without_clone_support_is_unavailable() -> None:
    class Opaque:
        checkpointer = InMemorySaver()

    assert _get_threadless_graph(Opaque()) is None
