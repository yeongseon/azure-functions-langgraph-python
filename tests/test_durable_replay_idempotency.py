"""Deterministic replay / at-least-once / side-effect contract for Durable runs (#810).

Everything here is in-process: no Azure host, no Durable Task backend. A tiny
``_Host`` plays the part of the Durable Functions host so a test can deliver the
*same* Activity payload more than once, and optionally discard the first result
to simulate "the Activity finished but its result was never durably recorded".

What these tests pin down (and what they do **not** claim):

* The adapter does not de-duplicate deliveries. Two deliveries of one payload
  run the graph twice; an external effect is therefore **at least once**.
* LangGraph checkpoints do not change that for a fresh invoke: a re-delivered
  payload with new input starts a new graph run on the same thread.
* The thread lock serializes concurrent attempts on one thread and is released
  on every exit path; it is not fencing (see #790) and does not de-duplicate.
* An application-level idempotency key derived from the stable ``run_id`` makes
  a tool call safe against redelivery *only* if the effect and its ledger record
  are atomic; the test shows the residual window explicitly.

Real host recycling / Activity attempts in Azure are out of scope (#811).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
from typing import Any, TypedDict
import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._orchestrator import ACTIVITY_NAME, run_orchestration
from azure_functions_langgraph.durable._state import DurableRunPayload, DurableRunResult
from azure_functions_langgraph.locks import (
    InProcessThreadLock,
    OwnershipGuard,
    ThreadLeaseLostError,
)
from azure_functions_langgraph.observability import RunContext

_SECRET = "postgresql://user:secret@db.internal/app"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
class _State(TypedDict, total=False):
    value: int


class _Effects:
    """Records every external effect a node performs (the 'tool call' log)."""

    def __init__(self) -> None:
        self.log: list[str] = []

    def count(self, name: str) -> int:
        return self.log.count(name)


class _Observer:
    """Captures terminal signals and the identifiers (never payloads)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None, str]] = []
        self.contexts: list[RunContext] = []  # complete, unprojected contexts

    def _rec(self, kind: str, ctx: RunContext) -> None:
        self.contexts.append(ctx)
        self.events.append((kind, ctx.run_id, ctx.thread_id, ctx.execution_id))

    def on_run_started(self, ctx: RunContext) -> None:
        self._rec("started", ctx)

    def on_run_completed(self, ctx: RunContext) -> None:
        self._rec("completed", ctx)

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self._rec("failed", ctx)

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self._rec(f"rejected:{reason}", ctx)

    @property
    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]


class _Reg:
    def __init__(self, graph: Any, name: str = "g") -> None:
        self.graph = graph
        self.name = name
        self.async_mode = False


def _payload(**kw: Any) -> DurableRunPayload:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "graph_name": "g",
        "input": {"value": 1},
        "thread_id": "t1",
    }
    base.update(kw)
    return DurableRunPayload(**base)


class _Host:
    """Minimal fake of the Durable host delivering the Activity."""

    def __init__(
        self,
        graph: Any,
        *,
        lock: Any = None,
        observer: Any = None,
    ) -> None:
        self.registry = {"g": _Reg(graph)}
        self.lock = lock if lock is not None else InProcessThreadLock()
        self.observer = observer if observer is not None else _Observer()
        self.recorded: list[dict[str, Any]] = []  # results durably recorded
        self.deliveries = 0

    async def deliver(self, payload: DurableRunPayload, *, record: bool = True) -> DurableRunResult:
        """One Activity attempt. ``record=False`` simulates a lost result."""
        self.deliveries += 1
        result = await execute_langgraph_run_impl(
            payload, registry=self.registry, thread_lock=self.lock, observer=self.observer
        )
        if record:
            self.recorded.append(result.to_dict())
        return result


def _graph(
    effects: _Effects,
    *,
    fail_after_a: bool = False,
    crash_in_a_after_effect: bool = False,
    checkpointer: Any = None,
) -> Any:
    """Two-node graph: ``a`` performs effect 'a', ``b`` performs effect 'b'."""

    def a(state: _State) -> _State:
        effects.log.append("a")
        if crash_in_a_after_effect:
            raise RuntimeError(f"a crashed after effect {_SECRET}")
        return {"value": state.get("value", 0) + 1}

    def b(state: _State) -> _State:
        if fail_after_a:
            raise RuntimeError("b failed")
        effects.log.append("b")
        return {"value": state.get("value", 0) + 10}

    builder = StateGraph(_State)
    builder.add_node("a", a)
    builder.add_node("b", b)
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------
# 1. Two-delivery harness; replay vs redelivery vs application retry
# --------------------------------------------------------------------------
class TestTwoDeliveries:
    async def test_redelivery_after_lost_result_repeats_external_effect(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects, checkpointer=MemorySaver()))
        p = _payload()

        first = await host.deliver(p, record=False)  # effect done, result lost
        second = await host.deliver(p)

        assert first.activity_status == second.activity_status == "success"
        # No de-duplication in the adapter: each node effect happened twice.
        assert effects.log == ["a", "b", "a", "b"]
        assert host.deliveries == 2
        assert len(host.recorded) == 1  # only the second result was recorded

    async def test_redelivery_reports_same_run_id_but_distinct_executions(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects))
        p = _payload(thread_id=None)

        await host.deliver(p, record=False)
        await host.deliver(p)

        obs: _Observer = host.observer
        assert obs.kinds == ["started", "completed", "started", "completed"]
        assert {e[1] for e in obs.events} == {"run-1"}
        exec_ids = [e[3] for e in obs.events if e[0] == "started"]
        assert len(set(exec_ids)) == 2  # attempts are distinguishable
        assert host.recorded[0]["run_id"] == "run-1"

    async def test_orchestrator_replay_never_executes_the_activity(self) -> None:
        """Deterministic replay re-drives the generator, not the Activity."""
        effects = _Effects()
        payload_dict = _payload().to_dict()

        class _Ctx:
            def __init__(self) -> None:
                self.scheduled: list[tuple[str, Any]] = []

            def get_input(self) -> Any:
                return payload_dict

            def call_activity(self, name: str, input_: Any) -> Any:
                self.scheduled.append((name, input_))
                return ("task", name)

        recorded = DurableRunResult.success("run-1", {"value": 12}).to_dict()
        outputs = []
        contexts = []
        for _ in range(3):  # three replays of one orchestration history
            ctx = _Ctx()
            gen = run_orchestration(ctx)
            task = next(gen)
            assert task == ("task", ACTIVITY_NAME)
            with pytest.raises(StopIteration) as stop:
                gen.send(recorded)  # history already holds the Activity result
            outputs.append(stop.value.value)
            contexts.append(ctx)

        assert outputs == [recorded] * 3
        assert all(c.scheduled == [(ACTIVITY_NAME, payload_dict)] for c in contexts)
        assert effects.log == []  # replay performed no external effect

    async def test_application_retry_is_a_new_run_not_a_redelivery(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects, checkpointer=MemorySaver()))

        await host.deliver(_payload(run_id="run-1"))
        await host.deliver(_payload(run_id="run-2"))  # explicit app-level retry

        run_ids = [e[1] for e in host.observer.events if e[0] == "started"]
        assert run_ids == ["run-1", "run-2"]
        assert effects.count("a") == 2  # also repeats the effect: caller must dedupe

    async def test_graph_failure_is_normalized_not_raised(self) -> None:
        """A graph error becomes a *result*; the host never sees an Activity failure."""
        effects = _Effects()
        host = _Host(_graph(effects, fail_after_a=True))
        result = await host.deliver(_payload(thread_id=None))
        assert result.activity_status == "error"
        assert host.observer.kinds == ["started", "failed"]


# --------------------------------------------------------------------------
# 2. Effects vs checkpoint persistence; concurrency
# --------------------------------------------------------------------------
class TestCheckpointBoundaries:
    async def test_effect_before_checkpoint_is_repeated_on_redelivery(self) -> None:
        """Node crashes after its effect, before its checkpoint is written."""
        effects = _Effects()
        saver = MemorySaver()
        host = _Host(_graph(effects, crash_in_a_after_effect=True, checkpointer=saver))
        p = _payload()

        r1 = await host.deliver(p, record=False)
        r2 = await host.deliver(p)

        assert r1.activity_status == r2.activity_status == "error"
        assert effects.log == ["a", "a"]  # nothing checkpointed -> effect repeated
        assert "b" not in effects.log

    async def test_effect_after_checkpoint_still_repeats_for_a_fresh_invoke(self) -> None:
        """Node ``a`` is checkpointed, ``b`` fails; redelivery with the same
        input re-enters at START on the same thread, so ``a`` runs again.

        The adapter always invokes with the payload input; it never resumes
        with ``None``. Checkpoints therefore are not an idempotency mechanism.
        """
        effects = _Effects()
        saver = MemorySaver()
        failing = _graph(effects, fail_after_a=True, checkpointer=saver)
        host = _Host(failing)
        p = _payload()

        r1 = await host.deliver(p, record=False)
        assert r1.activity_status == "error"
        persisted = saver.get_tuple({"configurable": {"thread_id": "t1"}})
        assert persisted is not None
        # Input checkpoint alone would also be non-None: require a's output
        # (1 + 1) to be the persisted channel value.
        assert persisted.checkpoint["channel_values"]["value"] == 2
        assert effects.log == ["a"]

        r2 = await host.deliver(p)
        assert r2.activity_status == "error"
        assert effects.log == ["a", "a"]  # observed: 'a' effect repeated

    async def test_effect_after_checkpoint_repeats_whole_graph_when_healthy(self) -> None:
        effects = _Effects()
        saver = MemorySaver()
        host = _Host(_graph(effects, checkpointer=saver))
        p = _payload()
        await host.deliver(p, record=False)
        before = len(list(saver.list({"configurable": {"thread_id": "t1"}})))
        await host.deliver(p)
        after = len(list(saver.list({"configurable": {"thread_id": "t1"}})))
        assert effects.log == ["a", "b", "a", "b"]
        assert after > before  # second delivery committed additional checkpoints

    async def test_concurrent_attempts_same_thread_run_effect_once(self) -> None:
        entered = threading.Event()
        proceed = threading.Event()
        effects = _Effects()

        def slow(state: _State) -> _State:
            effects.log.append("slow")
            entered.set()
            assert proceed.wait(5)
            return {"value": 1}

        builder = StateGraph(_State)
        builder.add_node("slow", slow)
        builder.add_edge(START, "slow")
        host = _Host(builder.compile(checkpointer=MemorySaver()))
        p = _payload()

        first = asyncio.create_task(host.deliver(p))
        assert await asyncio.to_thread(entered.wait, 5)
        second = await host.deliver(p)  # contends while the first holds the lock
        proceed.set()
        first_result = await first

        assert second.activity_status == "rejected"
        assert first_result.activity_status == "success"
        assert effects.log == ["slow"]  # contender never ran the tool
        assert host.observer.kinds == ["started", "rejected:lock_contention", "completed"]

    async def test_concurrent_attempts_on_different_threads_both_run(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects, checkpointer=MemorySaver()))
        r = await asyncio.gather(
            host.deliver(_payload(run_id="r1", thread_id="t1")),
            host.deliver(_payload(run_id="r2", thread_id="t2")),
        )
        assert [x.activity_status for x in r] == ["success", "success"]
        assert effects.count("a") == 2


# --------------------------------------------------------------------------
# 3. ThreadLock ownership: held / released / expired / reacquired
# --------------------------------------------------------------------------
class _ExpiringLock:
    """Fake lock whose lease can 'expire' mid-run and be taken by someone else."""

    def __init__(self) -> None:
        self._inner = InProcessThreadLock()
        self.lost = False
        self.acquired: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = self._inner.acquire(graph_name, thread_id, timeout)
        if token is not None:
            self.acquired.append(token)
        return token

    def release(self, graph_name: str, thread_id: str, token: str) -> None:
        self._inner.release(graph_name, thread_id, token)

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard:
        lock = self

        class _Guard:
            def check(self) -> None:
                if lock.lost:
                    raise ThreadLeaseLostError(
                        reason="lost", graph_name=graph_name, thread_id=thread_id
                    )

        return _Guard()


class TestThreadLockOwnership:
    async def test_lock_released_after_success_and_failure_so_thread_is_reacquirable(
        self,
    ) -> None:
        effects = _Effects()
        lock = InProcessThreadLock()
        ok = _Host(_graph(effects, checkpointer=MemorySaver()), lock=lock)
        bad = _Host(_graph(effects, fail_after_a=True, checkpointer=MemorySaver()), lock=lock)

        assert (await bad.deliver(_payload())).activity_status == "error"
        token = lock.acquire("g", "t1")  # failure path released it
        assert token is not None
        lock.release("g", "t1", token)
        assert (await ok.deliver(_payload())).activity_status == "success"
        token = lock.acquire("g", "t1")  # success path released it
        assert token is not None

    async def test_externally_held_lock_rejects_without_running_tools(self) -> None:
        effects = _Effects()
        lock = InProcessThreadLock()
        host = _Host(_graph(effects, checkpointer=MemorySaver()), lock=lock)
        held = lock.acquire("g", "t1")
        assert held is not None

        result = await host.deliver(_payload())

        assert result.activity_status == "rejected"
        assert effects.log == []
        assert host.observer.kinds == ["rejected:lock_contention"]

    async def test_no_lock_without_checkpointer_so_deliveries_do_not_contend(self) -> None:
        """Unchecked graphs take no lock: nothing serializes same-thread runs."""
        effects = _Effects()
        lock = InProcessThreadLock()
        host = _Host(_graph(effects), lock=lock)
        held = lock.acquire("g", "t1")
        assert held is not None
        result = await host.deliver(_payload())
        assert result.activity_status == "success"
        assert effects.log == ["a", "b"]

    async def test_lease_lost_during_run_reports_error_but_effects_already_happened(
        self,
    ) -> None:
        """Locally detected loss fails the delivery; it does not undo effects
        and a reacquired delivery repeats them (not fencing; see #790)."""
        effects = _Effects()
        lock = _ExpiringLock()

        def a(state: _State) -> _State:
            effects.log.append("a")
            lock.lost = True  # lease expires while the tool is running
            return {"value": 1}

        builder = StateGraph(_State)
        builder.add_node("a", a)
        builder.add_edge(START, "a")
        host = _Host(builder.compile(checkpointer=MemorySaver()), lock=lock)

        first = await host.deliver(_payload(), record=False)
        assert first.activity_status == "error"
        assert first.error is not None
        assert first.error["type"] == "ThreadLeaseLostError"
        assert effects.log == ["a"]  # effect happened despite the error result
        assert host.observer.kinds == ["started", "failed"]

        lock.lost = False  # new owner / reacquired lease
        second = await host.deliver(_payload())
        assert second.activity_status == "error"  # 'a' flips it lost again
        assert effects.log == ["a", "a"]
        assert len(lock.acquired) == 2  # reacquired by the redelivery

    async def test_lease_loss_message_is_fixed_and_leaks_no_ids(self) -> None:
        effects = _Effects()
        lock = _ExpiringLock()
        lock.lost = True
        host = _Host(_graph(effects, checkpointer=MemorySaver()), lock=lock)
        result = await host.deliver(_payload(thread_id="secret-thread"))
        blob = json.dumps(result.to_dict())
        assert "secret-thread" not in blob
        assert lock.acquired[0] not in blob


# --------------------------------------------------------------------------
# 4. Application-level idempotency pattern
# --------------------------------------------------------------------------
class _PaymentService:
    """Fake non-idempotent external system that can honor an idempotency key."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []  # every call, incl. deduplicated
        self.charges: list[tuple[str, int]] = []
        self._by_key: dict[str, str] = {}

    def charge(self, key: str, amount: int) -> str:
        self.calls.append((key, amount))
        if key in self._by_key:  # provider-side idempotency-key reconciliation
            return self._by_key[key]
        self.charges.append((key, amount))
        receipt = f"rcpt-{len(self.charges)}"
        self._by_key[key] = receipt
        return receipt


def _payment_graph(service: _PaymentService, *, key_from_run_id: bool) -> Any:
    def pay(state: _State, config: RunnableConfig) -> _State:
        run_id = config["configurable"]["app_run_id"]
        key = f"charge:{run_id}" if key_from_run_id else f"charge:{uuid.uuid4()}"
        service.charge(key, 5)
        return {"value": state.get("value", 0) + 1}

    builder = StateGraph(_State)
    builder.add_node("pay", pay)
    builder.add_edge(START, "pay")
    builder.add_edge("pay", END)
    return builder.compile(checkpointer=MemorySaver())


class TestIdempotencyPattern:
    async def test_key_derived_from_stable_run_id_dedupes_redelivery(self) -> None:
        service = _PaymentService()
        host = _Host(_payment_graph(service, key_from_run_id=True))
        p = _payload(config={"configurable": {"app_run_id": "run-1"}})

        r1 = await host.deliver(p, record=False)
        r2 = await host.deliver(p)

        assert r1.activity_status == r2.activity_status == "success"
        # The graph really called the provider twice with the same key...
        assert service.calls == [("charge:run-1", 5), ("charge:run-1", 5)]
        # ...and the provider recorded only one effect.
        assert service.charges == [("charge:run-1", 5)]

    async def test_per_attempt_random_key_does_not_dedupe(self) -> None:
        """What the pattern does NOT give you: a key minted per attempt."""
        service = _PaymentService()
        host = _Host(_payment_graph(service, key_from_run_id=False))
        p = _payload(config={"configurable": {"app_run_id": "run-1"}})

        await host.deliver(p, record=False)
        await host.deliver(p)

        assert len(service.charges) == 2

    async def test_distinct_application_retry_needs_its_own_business_key(self) -> None:
        """A new run_id yields a new key, so a deliberate retry charges again.
        Dedupe across *application* retries requires a business-level key."""
        service = _PaymentService()
        host = _Host(_payment_graph(service, key_from_run_id=True))
        await host.deliver(
            _payload(run_id="run-1", config={"configurable": {"app_run_id": "run-1"}})
        )
        await host.deliver(
            _payload(run_id="run-2", config={"configurable": {"app_run_id": "run-2"}})
        )
        assert len(service.charges) == 2

    async def test_local_ledger_has_a_residual_window_between_effect_and_record(self) -> None:
        """A caller-side ledger written *after* the effect is not atomic with it."""
        ledger: set[str] = set()
        effects = _Effects()

        def pay(state: _State) -> _State:
            key = "charge:run-1"
            if key in ledger:
                return {"value": 1}
            effects.log.append("charge")  # external effect
            if len(effects.log) == 1:
                raise RuntimeError("crash before ledger write")
            ledger.add(key)
            return {"value": 1}

        builder = StateGraph(_State)
        builder.add_node("pay", pay)
        builder.add_edge(START, "pay")
        host = _Host(builder.compile(checkpointer=MemorySaver()))
        p = _payload()
        await host.deliver(p, record=False)
        await host.deliver(p)
        assert effects.count("charge") == 2  # window: ledger alone is not exactly-once


# --------------------------------------------------------------------------
# 5. Observability and sanitized errors
# --------------------------------------------------------------------------
class TestObservabilityAndErrors:
    async def test_observer_context_carries_ids_only_no_payload_or_secret(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects, crash_in_a_after_effect=True, checkpointer=MemorySaver()))
        await host.deliver(_payload(input={"value": 1, "token": "tok-123"}))
        observer: _Observer = host.observer
        assert observer.contexts
        allowed = {
            "graph_name", "endpoint", "run_id", "thread_id", "started_at_ns",
            "ended_at_ns", "execution_id", "assistant_id", "invocation_id",
            "stream_mode", "transport", "has_checkpointer", "lock_backend",
            "trigger_type",
        }  # fmt: skip
        for ctx in observer.contexts:
            # Inspect the complete context, not the three-id projection.
            assert {f.name for f in dataclasses.fields(ctx)} <= allowed
            blob = json.dumps(dataclasses.asdict(ctx), default=str) + repr(ctx)
            assert "tok-123" not in blob
            assert _SECRET not in blob
        assert host.observer.events[0][1:3] == ("run-1", "t1")

    async def test_terminal_signal_exactly_one_per_delivery(self) -> None:
        effects = _Effects()
        host = _Host(_graph(effects, checkpointer=MemorySaver()))
        for _ in range(3):
            await host.deliver(_payload())
        terminals = [k for k in host.observer.kinds if k != "started"]
        assert terminals == ["completed"] * 3
        assert host.observer.kinds.count("started") == 3

    async def test_activity_result_stores_no_raw_exception_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The Activity output (Durable history) never carries exception text (#898).

        It holds only the exception class name and a fixed message; the raw
        exception is logged server-side. The HTTP status contract is unchanged.
        """
        from azure_functions_langgraph.durable._lifecycle import get_run_impl

        effects = _Effects()
        host = _Host(_graph(effects, crash_in_a_after_effect=True, checkpointer=MemorySaver()))
        with caplog.at_level(logging.ERROR, logger="azure_functions_langgraph.durable._activity"):
            result = await host.deliver(_payload(input={"value": 1, "token": "tok-123"}))

        assert result.error == {"type": "RuntimeError", "message": "The run failed."}
        history = json.dumps(result.to_dict())  # what lands in Durable history
        assert _SECRET not in history
        assert "crashed after effect" not in history
        assert "tok-123" not in history

        # Raw exception is available to operators in server-side logs only; the log
        # record carries identifiers, not the input payload.
        assert _SECRET in caplog.text
        assert "run_id=run-1" in caplog.text
        assert "tok-123" not in caplog.text

        class _Status:
            runtime_status = type("E", (), {"name": "Completed"})()
            output = result.to_dict()

        class _Client:
            async def start_new(
                self,
                orchestration_function_name: str,
                instance_id: str | None = None,
                client_input: Any = None,
            ) -> str:
                return instance_id or ""

            async def get_status(self, instance_id: str) -> Any:
                return _Status()

            async def terminate(self, instance_id: str, reason: str) -> None:
                return None

        code, body = await get_run_impl(_Client(), "run-1")
        assert code == 200
        assert _SECRET not in json.dumps(body)
        assert body["output"]["error"] == {"code": "run_failed", "message": "The run failed."}

    async def test_leak_check_fails_if_raw_exception_text_is_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation check: reinstating ``str(exc)`` must make the guard above fail."""
        from azure_functions_langgraph.durable import _state

        monkeypatch.setattr(
            _state,
            "serialize_error",
            lambda exc: {"type": type(exc).__name__, "message": str(exc)},
        )
        effects = _Effects()
        host = _Host(_graph(effects, crash_in_a_after_effect=True, checkpointer=MemorySaver()))
        result = await host.deliver(_payload())
        assert _SECRET in json.dumps(result.to_dict())  # the leak the real test rejects
