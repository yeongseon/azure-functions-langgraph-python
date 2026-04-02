"""Shared fixtures for tests."""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest


class FakeCompiledGraph:
    """Minimal mock of a LangGraph CompiledStateGraph for testing.

    Satisfies the :class:`LangGraphLike` protocol (has ``invoke`` and ``stream``).
    """

    def __init__(
        self,
        invoke_result: dict[str, Any] | None = None,
        stream_results: list[dict[str, Any]] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._invoke_result = invoke_result or {
            "messages": [{"role": "assistant", "content": "Hello!"}]
        }
        self._stream_results = stream_results or [
            {"messages": [{"role": "assistant", "content": "chunk1"}]},
            {"messages": [{"role": "assistant", "content": "chunk2"}]},
        ]
        self.checkpointer = checkpointer

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._invoke_result

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield from self._stream_results


class FakeInvokeOnlyGraph:
    """Graph that only supports invoke, not stream."""

    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": "ok"}


class FakeFailingGraph:
    """Graph that raises on invoke/stream for error path testing."""

    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("Graph execution failed")

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        raise RuntimeError("Stream execution failed")


@pytest.fixture
def fake_graph() -> FakeCompiledGraph:
    return FakeCompiledGraph()


@pytest.fixture
def fake_graph_with_checkpointer() -> FakeCompiledGraph:
    return FakeCompiledGraph(checkpointer=MagicMock())


@pytest.fixture
def fake_invoke_only_graph() -> FakeInvokeOnlyGraph:
    return FakeInvokeOnlyGraph()


@pytest.fixture
def fake_failing_graph() -> FakeFailingGraph:
    return FakeFailingGraph()
