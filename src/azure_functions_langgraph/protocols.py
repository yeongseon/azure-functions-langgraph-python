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
class LangGraphLike(InvocableGraph, StreamableGraph, Protocol):
    """Protocol combining invoke and stream — matches LangGraph's CompiledStateGraph."""

    pass
