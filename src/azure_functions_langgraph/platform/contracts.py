"""Pydantic v2 contracts mirroring the LangGraph Platform SDK wire shapes.

Every response model matches the ``TypedDict`` of the same name in
``langgraph_sdk.schema`` so that the official Python SDK client can
deserialise responses without conversion.

Request models use ``model_config = ConfigDict(extra="ignore")`` so
that unknown fields sent by newer SDK versions are silently dropped
instead of causing 422 errors.

The shapes target **langgraph-sdk ~0.1** (``langgraph_sdk.schema``
as of 2025-06).  Fields added in later SDK versions will be silently
dropped on request models and absent on response models until this
module is updated.

.. versionadded:: 0.3.0
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Type aliases — match langgraph_sdk.schema
# ---------------------------------------------------------------------------

Json = Union[dict[str, Any], None]
"""Metadata type alias matching ``langgraph_sdk.schema.Json``."""

RunStatus = Literal["pending", "running", "error", "success", "timeout", "interrupted"]
"""Status values for a Run."""

ThreadStatus = Literal["idle", "busy", "interrupted", "error"]
"""Status values for a Thread."""

MultitaskStrategy = Literal["reject", "interrupt", "rollback", "enqueue"]
"""Strategy for handling concurrent runs on the same thread."""

# ---------------------------------------------------------------------------
# Small supporting models
# ---------------------------------------------------------------------------


class Checkpoint(BaseModel):
    """Represents a checkpoint in the execution process."""

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: Optional[str] = None
    checkpoint_map: Optional[dict[str, Any]] = None


class Interrupt(BaseModel):
    """Represents an interruption in the execution flow."""

    value: Any = None
    id: str


# ---------------------------------------------------------------------------
# Core response models — strict (all required fields must be present)
# ---------------------------------------------------------------------------


class Assistant(BaseModel):
    """Mirrors ``langgraph_sdk.schema.Assistant``."""

    assistant_id: str
    graph_id: str
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    metadata: Json = None
    version: int = 1
    name: str
    description: Optional[str] = None
    updated_at: datetime
    context: dict[str, Any] = Field(default_factory=dict)


class Thread(BaseModel):
    """Mirrors ``langgraph_sdk.schema.Thread``."""

    thread_id: str
    created_at: datetime
    updated_at: datetime
    metadata: Json = None
    status: ThreadStatus = "idle"
    values: Json = None
    assistant_id: Optional[str] = None
    interrupts: dict[str, list[Interrupt]] = Field(default_factory=dict)


class ThreadTask(BaseModel):
    """Mirrors ``langgraph_sdk.schema.ThreadTask``.

    The ``state`` field uses ``Optional[dict[str, Any]]`` instead of a
    recursive ``ThreadState`` reference to avoid circular validation at
    the Pydantic level.  Full recursive typing can be added once the
    Platform layer needs deep subgraph introspection.
    """

    id: str
    name: str
    error: Optional[str] = None
    interrupts: list[Interrupt] = Field(default_factory=list)
    checkpoint: Optional[Checkpoint] = None
    state: Optional[dict[str, Any]] = None
    result: Optional[dict[str, Any]] = None


class ThreadState(BaseModel):
    """Mirrors ``langgraph_sdk.schema.ThreadState``."""

    values: Union[dict[str, Any], list[dict[str, Any]]]
    next: list[str]
    checkpoint: Checkpoint
    metadata: Json = None
    created_at: Optional[str] = None
    parent_checkpoint: Optional[Checkpoint] = None
    tasks: list[ThreadTask] = Field(default_factory=list)
    interrupts: list[Interrupt] = Field(default_factory=list)


class Run(BaseModel):
    """Mirrors ``langgraph_sdk.schema.Run``."""

    run_id: str
    thread_id: str
    assistant_id: str
    created_at: datetime
    updated_at: datetime
    status: RunStatus
    metadata: Json = None
    multitask_strategy: MultitaskStrategy = "reject"


# ---------------------------------------------------------------------------
# Request models — lenient (extra="ignore" for forward-compat)
# ---------------------------------------------------------------------------


class RunCreate(BaseModel):
    """Request body to create or stream a Run.

    Covers ``POST /threads/{thread_id}/runs`` and ``…/runs/stream``.
    Fields not present in the SDK ``RunCreate`` TypedDict (e.g.
    ``on_completion``, ``after_seconds``) are endpoint-level extensions
    accepted for forward-compatibility.
    """

    model_config = ConfigDict(extra="ignore")

    assistant_id: str
    thread_id: Optional[str] = None
    input: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None
    config: Optional[dict[str, Any]] = None
    context: Optional[dict[str, Any]] = None
    stream_mode: Union[str, list[str]] = "values"
    interrupt_before: Optional[Union[list[str], Literal["*"]]] = None
    interrupt_after: Optional[Union[list[str], Literal["*"]]] = None
    webhook: Optional[str] = None
    multitask_strategy: Optional[MultitaskStrategy] = None
    checkpoint_id: Optional[str] = None
    on_completion: Optional[str] = None
    after_seconds: Optional[float] = None
    if_not_exists: Optional[str] = None
    command: Optional[dict[str, Any]] = None
    feedback_keys: Optional[list[str]] = None


class ThreadCreate(BaseModel):
    """Request body to create a Thread.

    Covers ``POST /threads``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: Optional[dict[str, Any]] = None


class AssistantSearch(BaseModel):
    """Request body to search Assistants.

    Covers ``POST /assistants/search``.
    """

    model_config = ConfigDict(extra="ignore")

    graph_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    limit: int = Field(default=10, ge=1)
    offset: int = Field(default=0, ge=0)


class AssistantCount(BaseModel):
    """Request body to count Assistants.

    Covers ``POST /assistants/count``.
    """

    model_config = ConfigDict(extra="ignore")

    graph_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    name: Optional[str] = None


class ThreadUpdate(BaseModel):
    """Request body to update a Thread.

    Covers ``PATCH /threads/{thread_id}``.
    The SDK also sends a ``ttl`` field which is silently dropped
    (``extra="ignore"``).
    """

    model_config = ConfigDict(extra="ignore")

    metadata: Optional[dict[str, Any]] = None


class ThreadSearch(BaseModel):
    """Request body to search Threads.

    Covers ``POST /threads/search``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: Optional[dict[str, Any]] = None
    status: Optional[ThreadStatus] = None
    limit: int = Field(default=10, ge=1)
    offset: int = Field(default=0, ge=0)


class ThreadCount(BaseModel):
    """Request body to count Threads.

    Covers ``POST /threads/count``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: Optional[dict[str, Any]] = None
    status: Optional[ThreadStatus] = None

# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    # Type aliases
    "Json",
    "RunStatus",
    "ThreadStatus",
    "MultitaskStrategy",
    # Response models
    "Checkpoint",
    "Interrupt",
    "Assistant",
    "Thread",
    "ThreadTask",
    "ThreadState",
    "Run",
    # Request models
    "RunCreate",
    "ThreadCreate",
    "AssistantSearch",
    "AssistantCount",
    "ThreadUpdate",
    "ThreadSearch",
    "ThreadCount",
]
