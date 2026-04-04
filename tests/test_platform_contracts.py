"""Tests for platform API Pydantic contracts.

Validates that every model in ``platform.contracts`` round-trips correctly,
enforces required fields, and matches the LangGraph Platform SDK wire shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from azure_functions_langgraph.platform.contracts import (
    Assistant,
    AssistantCount,
    AssistantSearch,
    Checkpoint,
    Interrupt,
    MultitaskStrategy,
    Run,
    RunCreate,
    RunStatus,
    Thread,
    ThreadCreate,
    ThreadState,
    ThreadStatus,
    ThreadTask,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _checkpoint(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "thread_id": "t-1",
        "checkpoint_ns": "",
        "checkpoint_id": "cp-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


class TestCheckpoint:
    def test_minimal(self) -> None:
        cp = Checkpoint(thread_id="t-1")
        assert cp.thread_id == "t-1"
        assert cp.checkpoint_ns == ""
        assert cp.checkpoint_id is None
        assert cp.checkpoint_map is None

    def test_full(self) -> None:
        cp = Checkpoint(
            thread_id="t-1",
            checkpoint_ns="ns",
            checkpoint_id="cp-1",
            checkpoint_map={"key": "val"},
        )
        assert cp.checkpoint_ns == "ns"
        assert cp.checkpoint_id == "cp-1"
        assert cp.checkpoint_map == {"key": "val"}

    def test_missing_thread_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            Checkpoint()  # type: ignore[call-arg]

    def test_json_round_trip(self) -> None:
        cp = Checkpoint(thread_id="t-1", checkpoint_id="cp-1")
        data = cp.model_dump_json()
        restored = Checkpoint.model_validate_json(data)
        assert restored == cp


# ---------------------------------------------------------------------------
# Interrupt
# ---------------------------------------------------------------------------


class TestInterrupt:
    def test_minimal(self) -> None:
        intr = Interrupt(id="i-1")
        assert intr.id == "i-1"
        assert intr.value is None

    def test_with_value(self) -> None:
        intr = Interrupt(id="i-1", value={"question": "continue?"})
        assert intr.value == {"question": "continue?"}

    def test_arbitrary_value_type(self) -> None:
        """Interrupt.value should accept any type (str, int, list, dict, etc.)."""
        for val in ["text", 42, [1, 2], {"a": 1}, True, None]:
            intr = Interrupt(id="i-1", value=val)
            assert intr.value == val

    def test_missing_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            Interrupt(value="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Assistant
# ---------------------------------------------------------------------------


class TestAssistant:
    def test_minimal(self) -> None:
        a = Assistant(
            assistant_id="a-1",
            graph_id="chatbot",
            created_at=_NOW,
            name="chatbot",
            updated_at=_NOW,
        )
        assert a.assistant_id == "a-1"
        assert a.graph_id == "chatbot"
        assert a.config == {}
        assert a.metadata is None
        assert a.version == 1
        assert a.description is None
        assert a.context == {}

    def test_full(self) -> None:
        a = Assistant(
            assistant_id="a-1",
            graph_id="chatbot",
            config={"configurable": {"model": "gpt-4"}},
            created_at=_NOW,
            metadata={"env": "prod"},
            version=3,
            name="My Bot",
            description="A helpful chatbot",
            updated_at=_NOW,
            context={"user_id": "u-1"},
        )
        assert a.version == 3
        assert a.description == "A helpful chatbot"
        assert a.context == {"user_id": "u-1"}

    def test_missing_required_fields_raises(self) -> None:
        with pytest.raises(ValidationError):
            Assistant(assistant_id="a-1")  # type: ignore[call-arg]

    def test_json_round_trip(self) -> None:
        a = Assistant(
            assistant_id="a-1",
            graph_id="g",
            created_at=_NOW,
            name="g",
            updated_at=_NOW,
        )
        data = a.model_dump_json()
        restored = Assistant.model_validate_json(data)
        assert restored.assistant_id == "a-1"


# ---------------------------------------------------------------------------
# Thread
# ---------------------------------------------------------------------------


class TestThread:
    def test_minimal(self) -> None:
        t = Thread(thread_id="t-1", created_at=_NOW, updated_at=_NOW)
        assert t.thread_id == "t-1"
        assert t.status == "idle"
        assert t.metadata is None
        assert t.values is None
        assert t.interrupts == {}

    def test_with_status(self) -> None:
        t = Thread(thread_id="t-1", created_at=_NOW, updated_at=_NOW, status="busy")
        assert t.status == "busy"

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError):
            Thread(
                thread_id="t-1",
                created_at=_NOW,
                updated_at=_NOW,
                status="unknown",  # type: ignore[arg-type]
            )

    def test_with_interrupts(self) -> None:
        t = Thread(
            thread_id="t-1",
            created_at=_NOW,
            updated_at=_NOW,
            interrupts={"node_a": [Interrupt(id="i-1", value="pause")]},
        )
        assert len(t.interrupts["node_a"]) == 1
        assert t.interrupts["node_a"][0].id == "i-1"

    def test_json_round_trip(self) -> None:
        t = Thread(thread_id="t-1", created_at=_NOW, updated_at=_NOW, status="error")
        data = t.model_dump_json()
        restored = Thread.model_validate_json(data)
        assert restored.status == "error"


# ---------------------------------------------------------------------------
# ThreadTask
# ---------------------------------------------------------------------------


class TestThreadTask:
    def test_minimal(self) -> None:
        task = ThreadTask(id="task-1", name="agent")
        assert task.id == "task-1"
        assert task.name == "agent"
        assert task.error is None
        assert task.interrupts == []
        assert task.checkpoint is None
        assert task.state is None
        assert task.result is None

    def test_with_error(self) -> None:
        task = ThreadTask(id="task-1", name="agent", error="timeout")
        assert task.error == "timeout"

    def test_with_interrupts(self) -> None:
        task = ThreadTask(
            id="task-1",
            name="agent",
            interrupts=[Interrupt(id="i-1", value="q?")],
        )
        assert len(task.interrupts) == 1
        assert task.interrupts[0].id == "i-1"

    def test_with_checkpoint(self) -> None:
        task = ThreadTask(
            id="task-1",
            name="agent",
            checkpoint=Checkpoint(thread_id="t-1", checkpoint_id="cp-1"),
        )
        assert task.checkpoint is not None
        assert task.checkpoint.thread_id == "t-1"

    def test_state_as_dict(self) -> None:
        """state field accepts arbitrary dict (avoids circular ThreadState ref)."""
        task = ThreadTask(
            id="task-1",
            name="agent",
            state={"values": {"messages": []}, "next": []},
        )
        assert task.state is not None
        assert "values" in task.state

    def test_with_result(self) -> None:
        task = ThreadTask(id="task-1", name="agent", result={"output": "done"})
        assert task.result == {"output": "done"}


# ---------------------------------------------------------------------------
# ThreadState
# ---------------------------------------------------------------------------


class TestThreadState:
    def test_minimal(self) -> None:
        ts = ThreadState(
            values={"messages": []},
            next=["agent"],
            checkpoint=Checkpoint(thread_id="t-1"),
        )
        assert ts.values == {"messages": []}
        assert ts.next == ["agent"]
        assert ts.metadata is None
        assert ts.created_at is None
        assert ts.parent_checkpoint is None
        assert ts.tasks == []
        assert ts.interrupts == []

    def test_values_as_list(self) -> None:
        """values can be list[dict] for StateGraph with list-based channels."""
        ts = ThreadState(
            values=[{"key": "val1"}, {"key": "val2"}],
            next=[],
            checkpoint=Checkpoint(thread_id="t-1"),
        )
        assert isinstance(ts.values, list)
        assert len(ts.values) == 2

    def test_with_tasks(self) -> None:
        ts = ThreadState(
            values={},
            next=[],
            checkpoint=Checkpoint(thread_id="t-1"),
            tasks=[ThreadTask(id="task-1", name="node_a")],
        )
        assert len(ts.tasks) == 1
        assert ts.tasks[0].name == "node_a"

    def test_with_interrupts(self) -> None:
        ts = ThreadState(
            values={},
            next=[],
            checkpoint=Checkpoint(thread_id="t-1"),
            interrupts=[Interrupt(id="i-1", value="stop")],
        )
        assert len(ts.interrupts) == 1

    def test_with_parent_checkpoint(self) -> None:
        ts = ThreadState(
            values={},
            next=[],
            checkpoint=Checkpoint(thread_id="t-1", checkpoint_id="cp-2"),
            parent_checkpoint=Checkpoint(thread_id="t-1", checkpoint_id="cp-1"),
        )
        assert ts.parent_checkpoint is not None
        assert ts.parent_checkpoint.checkpoint_id == "cp-1"

    def test_created_at_is_string(self) -> None:
        """created_at is str | None per SDK spec (not datetime)."""
        ts = ThreadState(
            values={},
            next=[],
            checkpoint=Checkpoint(thread_id="t-1"),
            created_at="2025-06-15T12:00:00+00:00",
        )
        assert ts.created_at == "2025-06-15T12:00:00+00:00"

    def test_json_round_trip(self) -> None:
        ts = ThreadState(
            values={"k": "v"},
            next=["a", "b"],
            checkpoint=Checkpoint(thread_id="t-1", checkpoint_id="cp-1"),
            metadata={"step": 3},
            tasks=[ThreadTask(id="task-1", name="agent")],
        )
        data = ts.model_dump_json()
        restored = ThreadState.model_validate_json(data)
        assert restored.next == ["a", "b"]
        assert restored.tasks[0].name == "agent"

    def test_missing_required_fields_raises(self) -> None:
        with pytest.raises(ValidationError):
            ThreadState(values={})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class TestRun:
    def test_minimal(self) -> None:
        r = Run(
            run_id="r-1",
            thread_id="t-1",
            assistant_id="a-1",
            created_at=_NOW,
            updated_at=_NOW,
            status="pending",
        )
        assert r.run_id == "r-1"
        assert r.status == "pending"
        assert r.metadata is None
        assert r.multitask_strategy == "reject"

    def test_with_strategy(self) -> None:
        r = Run(
            run_id="r-1",
            thread_id="t-1",
            assistant_id="a-1",
            created_at=_NOW,
            updated_at=_NOW,
            status="running",
            multitask_strategy="enqueue",
        )
        assert r.multitask_strategy == "enqueue"

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError):
            Run(
                run_id="r-1",
                thread_id="t-1",
                assistant_id="a-1",
                created_at=_NOW,
                updated_at=_NOW,
                status="unknown",  # type: ignore[arg-type]
            )

    def test_invalid_strategy_raises(self) -> None:
        with pytest.raises(ValidationError):
            Run(
                run_id="r-1",
                thread_id="t-1",
                assistant_id="a-1",
                created_at=_NOW,
                updated_at=_NOW,
                status="success",
                multitask_strategy="invalid",  # type: ignore[arg-type]
            )

    def test_all_statuses(self) -> None:
        """Ensure all valid RunStatus values are accepted."""
        for status in ("pending", "running", "error", "success", "timeout", "interrupted"):
            r = Run(
                run_id="r-1",
                thread_id="t-1",
                assistant_id="a-1",
                created_at=_NOW,
                updated_at=_NOW,
                status=status,
            )
            assert r.status == status

    def test_json_round_trip(self) -> None:
        r = Run(
            run_id="r-1",
            thread_id="t-1",
            assistant_id="a-1",
            created_at=_NOW,
            updated_at=_NOW,
            status="success",
            metadata={"source": "test"},
        )
        data = r.model_dump_json()
        restored = Run.model_validate_json(data)
        assert restored.metadata == {"source": "test"}


# ---------------------------------------------------------------------------
# RunCreate (request model)
# ---------------------------------------------------------------------------


class TestRunCreate:
    def test_minimal_with_new_fields(self) -> None:
        rc = RunCreate(assistant_id="a-1")
        assert rc.assistant_id == "a-1"
        assert rc.thread_id is None
        assert rc.input is None
        assert rc.context is None
        assert rc.checkpoint_id is None
        assert rc.stream_mode == "values"
        assert rc.interrupt_before is None
        assert rc.multitask_strategy is None

    def test_with_thread_id_and_context(self) -> None:
        rc = RunCreate(
            assistant_id="a-1",
            thread_id="t-1",
            context={"user": "u-1"},
            checkpoint_id="cp-1",
        )
        assert rc.thread_id == "t-1"
        assert rc.context == {"user": "u-1"}
        assert rc.checkpoint_id == "cp-1"

    def test_with_input_and_config(self) -> None:
        rc = RunCreate(
            assistant_id="a-1",
            input={"messages": [{"role": "human", "content": "hi"}]},
            config={"configurable": {"thread_id": "t-1"}},
        )
        assert rc.input is not None
        assert rc.config is not None

    def test_stream_mode_list(self) -> None:
        rc = RunCreate(assistant_id="a-1", stream_mode=["values", "updates"])
        assert rc.stream_mode == ["values", "updates"]

    def test_interrupt_before_star(self) -> None:
        """SDK allows interrupt_before='*' to mean all nodes."""
        rc = RunCreate(assistant_id="a-1", interrupt_before="*")
        assert rc.interrupt_before == "*"

    def test_interrupt_before_list(self) -> None:
        rc = RunCreate(assistant_id="a-1", interrupt_before=["agent", "tool"])
        assert rc.interrupt_before == ["agent", "tool"]

    def test_extra_fields_ignored(self) -> None:
        """Unknown fields from newer SDK versions should be silently dropped."""
        rc = RunCreate.model_validate(
            {"assistant_id": "a-1", "future_field": True, "another": 42}
        )
        assert rc.assistant_id == "a-1"
        assert not hasattr(rc, "future_field")

    def test_with_optional_scheduling(self) -> None:
        rc = RunCreate(
            assistant_id="a-1",
            after_seconds=5.0,
            on_completion="notify",
            if_not_exists="create",
        )
        assert rc.after_seconds == 5.0
        assert rc.on_completion == "notify"
        assert rc.if_not_exists == "create"

    def test_missing_assistant_id_raises(self) -> None:
        with pytest.raises(ValidationError):
            RunCreate()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ThreadCreate (request model)
# ---------------------------------------------------------------------------


class TestThreadCreate:
    def test_empty(self) -> None:
        tc = ThreadCreate()
        assert tc.metadata is None

    def test_with_metadata(self) -> None:
        tc = ThreadCreate(metadata={"user_id": "u-1"})
        assert tc.metadata == {"user_id": "u-1"}

    def test_extra_fields_ignored(self) -> None:
        tc = ThreadCreate.model_validate({"metadata": {}, "if_exists": "raise"})
        assert not hasattr(tc, "if_exists")


# ---------------------------------------------------------------------------
# AssistantSearch (request model)
# ---------------------------------------------------------------------------


class TestAssistantSearch:
    def test_defaults(self) -> None:
        s = AssistantSearch()
        assert s.graph_id is None
        assert s.metadata is None
        assert s.name is None
        assert s.limit == 10
        assert s.offset == 0
    def test_with_filters(self) -> None:
        s = AssistantSearch(graph_id="chatbot", limit=5, offset=20)
        assert s.graph_id == "chatbot"
        assert s.limit == 5
        assert s.offset == 20

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            AssistantSearch(limit=0)

    def test_offset_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            AssistantSearch(offset=-1)

    def test_extra_fields_ignored(self) -> None:
        s = AssistantSearch.model_validate({"graph_id": "g", "some_new_field": True})
        assert s.graph_id == "g"
        assert not hasattr(s, "some_new_field")


class TestAssistantCount:
    def test_defaults(self) -> None:
        c = AssistantCount()
        assert c.graph_id is None
        assert c.metadata is None
        assert c.name is None

    def test_with_filters(self) -> None:
        c = AssistantCount(graph_id="chatbot", name="bot")
        assert c.graph_id == "chatbot"
        assert c.name == "bot"

    def test_extra_fields_ignored(self) -> None:
        c = AssistantCount.model_validate({"graph_id": "g", "some_new_field": True})
        assert c.graph_id == "g"
        assert not hasattr(c, "some_new_field")

# ---------------------------------------------------------------------------
# Type alias sanity checks
# ---------------------------------------------------------------------------


class TestTypeAliases:
    def test_run_status_values(self) -> None:
        """RunStatus should accept all valid literal values."""
        valid: list[RunStatus] = [
            "pending", "running", "error", "success", "timeout", "interrupted",
        ]
        assert len(valid) == 6

    def test_thread_status_values(self) -> None:
        valid: list[ThreadStatus] = ["idle", "busy", "interrupted", "error"]
        assert len(valid) == 4

    def test_multitask_strategy_values(self) -> None:
        valid: list[MultitaskStrategy] = ["reject", "interrupt", "rollback", "enqueue"]
        assert len(valid) == 4
