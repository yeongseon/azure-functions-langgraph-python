"""Pydantic v2 contracts mirroring the LangGraph Platform SDK wire shapes.

Every response model matches the ``TypedDict`` of the same name in
``langgraph_sdk.schema`` so that the official Python SDK client can
deserialise responses without conversion.

Request models use ``model_config = ConfigDict(extra="ignore")`` so that
genuinely-unknown fields sent by newer SDK versions are silently dropped
instead of causing 422 errors. This is deliberate forward-compatibility:

* **Known-but-unsupported** request fields (e.g. ``interrupt_before``,
  ``webhook``, ``multitask_strategy`` other than ``reject``) are declared
  explicitly here and rejected with ``501 Not Implemented`` at the route
  layer (see ``platform/_runs.py`` and ``COMPATIBILITY.md``) rather than
  silently ignored — the caller always learns the feature is unsupported.
* **Truly-unknown** future fields are still dropped by ``extra="ignore"`` so a
  newer SDK client does not break against an older server, but the route
  layer now logs a warning and emits a :class:`UserWarning` for them so the
  drift is observable. Set ``AZFUNC_LANGGRAPH_PLATFORM_STRICT`` to reject
  unknown fields with ``400`` instead (opt-in strict mode).

The shapes target the **langgraph-sdk** ``>=0.2.2,<0.4`` wire format
(``langgraph_sdk.schema``). This range is the single source of truth shared
with ``pyproject.toml`` (dev/test pin) and ``COMPATIBILITY.md``; a drift
guard in ``tests/test_sdk_contracts.py`` fails if the installed SDK falls
outside it. Fields added in later SDK versions are dropped on request
models and absent on response models until this module is updated for a
new supported range.

.. versionadded:: 0.3.0
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Type aliases — match langgraph_sdk.schema
# ---------------------------------------------------------------------------

Json = Union[dict[str, Any], None]  # noqa: UP007
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
    checkpoint_id: str | None = None
    checkpoint_map: dict[str, Any] | None = None


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
    description: str | None = None
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
    assistant_id: str | None = None
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
    error: str | None = None
    interrupts: list[Interrupt] = Field(default_factory=list)
    checkpoint: Checkpoint | None = None
    state: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class ThreadState(BaseModel):
    """Mirrors ``langgraph_sdk.schema.ThreadState``."""

    values: dict[str, Any] | list[dict[str, Any]]
    next: list[str]
    checkpoint: Checkpoint
    metadata: Json = None
    created_at: str | None = None
    parent_checkpoint: Checkpoint | None = None
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
    thread_id: str | None = None
    input: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    stream_mode: str | list[str] = "values"
    interrupt_before: list[str] | Literal["*"] | None = None
    interrupt_after: list[str] | Literal["*"] | None = None
    webhook: str | None = None
    multitask_strategy: MultitaskStrategy | None = None
    checkpoint_id: str | None = None
    on_completion: str | None = None
    after_seconds: float | None = None
    if_not_exists: str | None = None
    command: dict[str, Any] | None = None
    feedback_keys: list[str] | None = None


class ThreadCreate(BaseModel):
    """Request body to create a Thread.

    Covers ``POST /threads``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, Any] | None = None


class AssistantSearch(BaseModel):
    """Request body to search Assistants.

    Covers ``POST /assistants/search``.
    """

    model_config = ConfigDict(extra="ignore")

    graph_id: str | None = None
    metadata: dict[str, Any] | None = None
    name: str | None = None
    limit: int = Field(default=10, ge=1)
    offset: int = Field(default=0, ge=0)


class AssistantCount(BaseModel):
    """Request body to count Assistants.

    Covers ``POST /assistants/count``.
    """

    model_config = ConfigDict(extra="ignore")

    graph_id: str | None = None
    metadata: dict[str, Any] | None = None
    name: str | None = None


class ThreadUpdate(BaseModel):
    """Request body to update a Thread.

    Covers ``PATCH /threads/{thread_id}``.
    The SDK also sends a ``ttl`` field which is not supported: it is dropped
    (``extra="ignore"``) but now surfaces a warning and a :class:`UserWarning`
    at the route layer, or a ``400`` when
    ``AZFUNC_LANGGRAPH_PLATFORM_STRICT`` is set.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, Any] | None = None


class ThreadSearch(BaseModel):
    """Request body to search Threads.

    Covers ``POST /threads/search``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, Any] | None = None
    status: ThreadStatus | None = None
    limit: int = Field(default=10, ge=1)
    offset: int = Field(default=0, ge=0)


class ThreadCount(BaseModel):
    """Request body to count Threads.

    Covers ``POST /threads/count``.
    """

    model_config = ConfigDict(extra="ignore")

    metadata: dict[str, Any] | None = None
    status: ThreadStatus | None = None


class ThreadStateUpdate(BaseModel):
    """Request body to update thread state.

    Covers ``POST /threads/{thread_id}/state``.
    The SDK always sends ``values``; we require it for safety.
    """

    model_config = ConfigDict(extra="ignore")

    values: dict[str, Any] | list[dict[str, Any]]
    as_node: str | None = None
    checkpoint_id: str | None = None
    checkpoint: dict[str, Any] | None = None


class ThreadHistoryRequest(BaseModel):
    """Request body to query thread state history.

    Covers ``POST /threads/{thread_id}/history``.
    """

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=10, ge=1)
    before: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None


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
    "ThreadStateUpdate",
    "ThreadHistoryRequest",
]
