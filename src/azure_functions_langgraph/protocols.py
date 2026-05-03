"""Protocol interfaces for graph compatibility.

Uses ``typing.Protocol`` so any object with the right methods works,
without requiring a hard import of ``langgraph``.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class InvocableGraph(Protocol):
    """Protocol for a graph that supports synchronous invocation."""

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = ...) -> Any: ...


@runtime_checkable
class StreamableGraph(Protocol):
    """Protocol for a graph that supports synchronous streaming."""

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = ...,
        stream_mode: str = ...,
    ) -> Iterator[Any]: ...


@runtime_checkable
class StatefulGraph(Protocol):
    """Protocol for a graph that supports state retrieval via a checkpointer."""

    def get_state(self, config: dict[str, Any]) -> Any: ...


@runtime_checkable
class UpdatableStateGraph(Protocol):
    """Protocol for a graph that supports state updates via a checkpointer."""

    def update_state(
        self,
        config: dict[str, Any],
        values: dict[str, Any] | list[dict[str, Any]] | None,
        *,
        as_node: str | None = ...,
    ) -> Any: ...


@runtime_checkable
class StateHistoryGraph(Protocol):
    """Protocol for a graph that supports state history retrieval via a checkpointer."""

    def get_state_history(self, config: dict[str, Any]) -> Any: ...


@runtime_checkable
class CloneableGraph(Protocol):
    """Protocol for a graph that supports cloning with updated configuration.

    Used by threadless runs to create a checkpoint-disabled copy of the graph.
    Matches LangGraph's ``CompiledStateGraph.copy(update=...)`` interface.
    """

    def copy(self, *, update: dict[str, Any] | None = ...) -> Any: ...


@runtime_checkable
class LangGraphLike(InvocableGraph, StreamableGraph, Protocol):
    """Protocol combining invoke and stream — matches LangGraph's CompiledStateGraph."""

    pass
