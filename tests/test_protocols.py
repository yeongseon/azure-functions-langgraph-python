"""Tests for protocol interfaces."""

from __future__ import annotations

from azure_functions_langgraph.protocols import (
    InvocableGraph,
    LangGraphLike,
    StreamableGraph,
)
from tests.conftest import FakeCompiledGraph, FakeInvokeOnlyGraph


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
