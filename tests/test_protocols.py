"""Tests for protocol interfaces."""

from __future__ import annotations

from typing import Any

from azure_functions_langgraph.protocols import (
    CloneableGraph,
    InvocableGraph,
    LangGraphLike,
    StatefulGraph,
    StreamableGraph,
)
from tests.conftest import (
    FakeCompiledGraph,
    FakeInvokeOnlyGraph,
    FakeStatefulGraph,
)


class TestProtocols:
    def test_fake_graph_satisfies_langgraph_like(self) -> None:
        graph = FakeCompiledGraph()
        assert isinstance(graph, InvocableGraph)
        assert isinstance(graph, StreamableGraph)
        assert isinstance(graph, LangGraphLike)

    def test_invoke_only_graph_satisfies_invocable(self) -> None:
        graph = FakeInvokeOnlyGraph()
        assert isinstance(graph, InvocableGraph)

    def test_invoke_only_graph_not_streamable(self) -> None:
        graph = FakeInvokeOnlyGraph()
        assert not isinstance(graph, StreamableGraph)

    def test_plain_object_not_invocable(self) -> None:
        assert not isinstance(object(), InvocableGraph)

    def test_plain_object_not_streamable(self) -> None:
        assert not isinstance(object(), StreamableGraph)

    def test_stateful_graph_satisfies_protocol(self) -> None:
        graph = FakeStatefulGraph()
        assert isinstance(graph, StatefulGraph)
        assert isinstance(graph, InvocableGraph)

    def test_fake_graph_not_stateful(self) -> None:
        graph = FakeCompiledGraph()
        assert not isinstance(graph, StatefulGraph)

    def test_copyable_graph_satisfies_cloneable(self) -> None:
        class _Copyable:
            def copy(self, *, update: dict[str, Any] | None = None) -> _Copyable:
                return _Copyable()

        assert isinstance(_Copyable(), CloneableGraph)

    def test_plain_object_not_cloneable(self) -> None:
        assert not isinstance(object(), CloneableGraph)

    def test_fake_compiled_graph_not_cloneable(self) -> None:
        """FakeCompiledGraph has no copy() method — should NOT satisfy CloneableGraph."""
        graph = FakeCompiledGraph()
        assert not isinstance(graph, CloneableGraph)
