"""Deterministic tests for the Durable async-run control plane (issue #408).

Covers the pure activity/orchestrator/lifecycle logic plus the runtime HTTP
adapters and DFApp registration, all with in-process fakes — no Azure host and
no Durable Task backend required.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
from azure_functions_langgraph.durable._lifecycle import (
    cancel_run_impl,
    create_run_impl,
    get_run_impl,
    runtime_status_str,
)
from azure_functions_langgraph.durable._orchestrator import (
    ACTIVITY_NAME,
    run_orchestration,
)
from azure_functions_langgraph.durable._runtime import (
    ORCHESTRATOR_NAME,
    DurableRuntimeDeps,
    _parse_body,
    cancel_run_http,
    create_durable_function_app,
    create_run_http,
    get_run_http,
    register_durable_runtime,
)
from azure_functions_langgraph.durable._state import (
    DurableRunPayload,
    normalize_status,
)
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import RunContext


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeGraph:
    def __init__(self, *, checkpointer: Any = None, result: Any = None, boom: bool = False) -> None:
        self.checkpointer = checkpointer
        self._result = result if result is not None else {"ok": True}
        self._boom = boom
        self.calls: list[tuple[Any, Any]] = []

    def invoke(self, input: Any, config: Any = None) -> Any:
        self.calls.append((input, config))
        if self._boom:
            raise RuntimeError("boom")
        return self._result


class FakeAsyncGraph:
    def __init__(self, *, checkpointer: Any = None, result: Any = None) -> None:
        self.checkpointer = checkpointer
        self._result = result if result is not None else {"ok": True}
        self.calls: list[tuple[Any, Any]] = []

    async def ainvoke(self, input: Any, config: Any = None) -> Any:
        self.calls.append((input, config))
        return self._result


class FakeReg:
    def __init__(self, graph: Any, name: str, async_mode: bool = False) -> None:
        self.graph = graph
        self.name = name
        self.async_mode = async_mode


class RecordingObserver:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append("completed")

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append("failed")

    def on_run_rejected(self, ctx: RunContext, reason: str) -> None:
        self.events.append("rejected")


class FakeStatus:
    def __init__(self, runtime_status: Any, output: Any = None) -> None:
        self.runtime_status = runtime_status
        self.output = output


class _Enum:
    """Mimics an OrchestrationRuntimeStatus enum member (has ``.name``)."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeDurableClient:
    def __init__(self, status: Optional[FakeStatus] = None) -> None:
        self._status = status
        self.started: list[tuple[str, Optional[str], Any]] = []
        self.terminated: list[tuple[str, str]] = []

    async def start_new(
        self,
        orchestration_function_name: str,
        instance_id: Optional[str] = None,
        client_input: Any = None,
    ) -> str:
        self.started.append((orchestration_function_name, instance_id, client_input))
        return instance_id or "generated-id"

    async def get_status(self, instance_id: str) -> Optional[FakeStatus]:
        return self._status

    async def terminate(self, instance_id: str, reason: str) -> None:
        self.terminated.append((instance_id, reason))


def _payload(**kw: Any) -> DurableRunPayload:
    base: dict[str, Any] = {"run_id": "r1", "graph_name": "echo", "input": {"x": 1}}
    base.update(kw)
    return DurableRunPayload(**base)


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------
class TestActivity:
    async def test_success_sync_graph(self) -> None:
        graph = FakeGraph(result={"done": True})
        reg = FakeReg(graph, "echo")
        obs = RecordingObserver()
        result = await execute_langgraph_run_impl(
            _payload(), registry={"echo": reg}, thread_lock=InProcessThreadLock(), observer=obs
        )
        assert result.activity_status == "success"
        assert result.result == {"done": True}
        assert obs.events == ["started", "completed"]

    async def test_success_async_graph(self) -> None:
        graph = FakeAsyncGraph(result={"a": 2})
        reg = FakeReg(graph, "echo", async_mode=True)
        result = await execute_langgraph_run_impl(
            _payload(), registry={"echo": reg}, thread_lock=InProcessThreadLock()
        )
        assert result.activity_status == "success"
        assert result.result == {"a": 2}

    async def test_unknown_graph_returns_failure(self) -> None:
        result = await execute_langgraph_run_impl(
            _payload(graph_name="missing"),
            registry={},
            thread_lock=InProcessThreadLock(),
        )
        assert result.activity_status == "error"
        assert result.error is not None
        assert result.error["type"] == "KeyError"

    async def test_graph_failure_captured(self) -> None:
        graph = FakeGraph(boom=True)
        reg = FakeReg(graph, "echo")
        obs = RecordingObserver()
        result = await execute_langgraph_run_impl(
            _payload(), registry={"echo": reg}, thread_lock=InProcessThreadLock(), observer=obs
        )
        assert result.activity_status == "error"
        assert result.error == {"type": "RuntimeError", "message": "boom"}
        assert obs.events == ["started", "failed"]

    async def test_lock_used_with_checkpointer_and_thread_id(self) -> None:
        graph = FakeGraph(checkpointer=object(), result={"ok": 1})
        reg = FakeReg(graph, "echo")
        lock = InProcessThreadLock()
        result = await execute_langgraph_run_impl(
            _payload(thread_id="t1"), registry={"echo": reg}, thread_lock=lock
        )
        assert result.activity_status == "success"
        # config derived with thread_id
        assert graph.calls[0][1] == {"configurable": {"thread_id": "t1"}}

    async def test_lock_contention_returns_rejected(self) -> None:
        graph = FakeGraph(checkpointer=object())
        reg = FakeReg(graph, "echo")
        lock = InProcessThreadLock()
        # Pre-acquire the same (name, thread_id) so the activity cannot get it.
        assert lock.acquire("echo", "t1")
        obs = RecordingObserver()
        result = await execute_langgraph_run_impl(
            _payload(thread_id="t1"), registry={"echo": reg}, thread_lock=lock, observer=obs
        )
        assert result.activity_status == "rejected"
        assert obs.events == ["rejected"]

    async def test_async_graph_with_config(self) -> None:
        graph = FakeAsyncGraph(checkpointer=object(), result={"a": 3})
        reg = FakeReg(graph, "echo", async_mode=True)
        result = await execute_langgraph_run_impl(
            _payload(thread_id="t1"), registry={"echo": reg}, thread_lock=InProcessThreadLock()
        )
        assert result.result == {"a": 3}
        assert graph.calls[0][1] == {"configurable": {"thread_id": "t1"}}

    async def test_explicit_config_passthrough(self) -> None:
        graph = FakeGraph(checkpointer=object())
        reg = FakeReg(graph, "echo")
        cfg = {"configurable": {"thread_id": "t9", "extra": 1}}
        await execute_langgraph_run_impl(
            _payload(thread_id="t9", config=cfg),
            registry={"echo": reg},
            thread_lock=InProcessThreadLock(),
        )
        assert graph.calls[0][1] == cfg


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
class FakeContext:
    def __init__(self, input_: Any) -> None:
        self._input = input_
        self.calls: list[tuple[str, Any]] = []

    def get_input(self) -> Any:
        return self._input

    def call_activity(self, name: str, input_: Any) -> Any:
        self.calls.append((name, input_))
        return f"task:{name}"


class TestOrchestrator:
    def test_yields_single_activity_and_returns_result(self) -> None:
        ctx = FakeContext({"run_id": "r1"})
        gen = run_orchestration(ctx)
        task = next(gen)
        assert task == f"task:{ACTIVITY_NAME}"
        assert ctx.calls == [(ACTIVITY_NAME, {"run_id": "r1"})]
        # Feed the activity result back; generator returns it.
        with pytest.raises(StopIteration) as exc:
            gen.send({"activity_status": "success"})
        assert exc.value.value == {"activity_status": "success"}


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
class TestRuntimeStatusStr:
    def test_none_status(self) -> None:
        assert runtime_status_str(None) is None

    def test_no_runtime_status(self) -> None:
        assert runtime_status_str(FakeStatus(None)) is None

    def test_enum_member(self) -> None:
        assert runtime_status_str(FakeStatus(_Enum("Running"))) == "Running"

    def test_plain_string(self) -> None:
        assert runtime_status_str(FakeStatus("Completed")) == "Completed"


class TestCreateRun:
    async def test_mints_run_id(self) -> None:
        client = FakeDurableClient()
        code, body = await create_run_impl(
            client, ORCHESTRATOR_NAME, {"input": {"x": 1}}, graph_name="echo"
        )
        assert code == 202
        assert body["status"] == "pending"
        assert body["run_id"]
        assert client.started[0][0] == ORCHESTRATOR_NAME

    async def test_uses_provided_run_id(self) -> None:
        client = FakeDurableClient()
        code, body = await create_run_impl(
            client, ORCHESTRATOR_NAME, {"run_id": "fixed", "input": 1}, graph_name="echo"
        )
        assert body["run_id"] == "fixed"
        assert client.started[0][1] == "fixed"

    async def test_derives_thread_id_from_config(self) -> None:
        client = FakeDurableClient()
        await create_run_impl(
            client,
            ORCHESTRATOR_NAME,
            {"input": 1, "config": {"configurable": {"thread_id": "t5"}}},
            graph_name="echo",
        )
        sent_payload = client.started[0][2]
        assert sent_payload["thread_id"] == "t5"


class TestGetRun:
    async def test_not_found(self) -> None:
        code, body = await get_run_impl(FakeDurableClient(status=None), "r1")
        assert code == 404
        assert body["error"] == "run not found"

    async def test_completed_success(self) -> None:
        output = {"activity_status": "success", "result": {"y": 1}}
        client = FakeDurableClient(status=FakeStatus(_Enum("Completed"), output))
        code, body = await get_run_impl(client, "r1")
        assert code == 200
        assert body["status"] == "success"
        assert body["output"] == output

    async def test_running(self) -> None:
        client = FakeDurableClient(status=FakeStatus(_Enum("Running")))
        code, body = await get_run_impl(client, "r1")
        assert code == 200
        assert body["status"] == "running"


class TestCancelRun:
    async def test_not_found(self) -> None:
        code, body = await cancel_run_impl(FakeDurableClient(status=None), "r1")
        assert code == 404

    async def test_terminates(self) -> None:
        client = FakeDurableClient(status=FakeStatus(_Enum("Running")))
        code, body = await cancel_run_impl(client, "r1")
        assert code == 202
        assert body["status"] == "cancelled"
        assert client.terminated == [("r1", "cancelled by client")]


# --------------------------------------------------------------------------
# State: normalize_status edge cases not covered elsewhere
# --------------------------------------------------------------------------
class TestNormalizeStatus:
    @pytest.mark.parametrize(
        "runtime,output,expected",
        [
            ("Pending", None, "pending"),
            ("", None, "running"),
            ("Running", None, "running"),
            ("ContinuedAsNew", None, "running"),
            ("Suspended", None, "running"),
            ("Completed", {"activity_status": "success"}, "success"),
            ("Completed", {"activity_status": "error"}, "error"),
            ("Completed", {"activity_status": "rejected"}, "error"),
            ("Completed", None, "success"),
            ("Failed", None, "error"),
            ("Terminated", None, "cancelled"),
            ("Canceled", None, "cancelled"),
            ("Cancelled", None, "cancelled"),
            ("Weird", None, "running"),
            (None, None, "running"),
        ],
    )
    def test_mapping(self, runtime: Any, output: Any, expected: str) -> None:
        assert normalize_status(runtime, output) == expected


# --------------------------------------------------------------------------
# Runtime HTTP adapters
# --------------------------------------------------------------------------
def _req(
    *, route_params: dict[str, str], body: bytes = b"", method: str = "POST"
) -> func.HttpRequest:
    return func.HttpRequest(method=method, url="/api/x", body=body, route_params=route_params)


class TestParseBody:
    def test_empty_is_empty_object(self) -> None:
        body, err = _parse_body(_req(route_params={}, body=b""), max_bytes=100)
        assert body == {} and err is None

    def test_too_large(self) -> None:
        body, err = _parse_body(_req(route_params={}, body=b"x" * 50), max_bytes=10)
        assert body is None and err is not None and "exceeds" in err

    def test_invalid_json(self) -> None:
        body, err = _parse_body(_req(route_params={}, body=b"{bad"), max_bytes=100)
        assert body is None and "valid JSON" in (err or "")

    def test_non_object(self) -> None:
        body, err = _parse_body(_req(route_params={}, body=b"[1,2]"), max_bytes=100)
        assert body is None and "JSON object" in (err or "")

    def test_valid_object(self) -> None:
        body, err = _parse_body(
            _req(route_params={}, body=json.dumps({"a": 1}).encode()), max_bytes=100
        )
        assert body == {"a": 1} and err is None


class TestCreateRunHttp:
    async def test_unknown_graph_404(self) -> None:
        reg = FakeReg(FakeGraph(), "echo")
        resp = await create_run_http(
            FakeDurableClient(),
            _req(route_params={"name": "nope"}),
            registry={"echo": reg},
        )
        assert resp.status_code == 404

    async def test_bad_body_400(self) -> None:
        reg = FakeReg(FakeGraph(), "echo")
        resp = await create_run_http(
            FakeDurableClient(),
            _req(route_params={"name": "echo"}, body=b"[1]"),
            registry={"echo": reg},
        )
        assert resp.status_code == 400

    async def test_success_202(self) -> None:
        reg = FakeReg(FakeGraph(), "echo")
        client = FakeDurableClient()
        resp = await create_run_http(
            client,
            _req(route_params={"name": "echo"}, body=json.dumps({"input": {"x": 1}}).encode()),
            registry={"echo": reg},
        )
        assert resp.status_code == 202
        payload = json.loads(resp.get_body())
        assert payload["status"] == "pending"


class TestGetCancelHttp:
    async def test_get_run_http(self) -> None:
        client = FakeDurableClient(status=FakeStatus(_Enum("Running")))
        resp = await get_run_http(client, _req(route_params={"run_id": "r1"}, method="GET"))
        assert resp.status_code == 200
        assert json.loads(resp.get_body())["status"] == "running"

    async def test_cancel_run_http(self) -> None:
        client = FakeDurableClient(status=FakeStatus(_Enum("Running")))
        resp = await cancel_run_http(client, _req(route_params={"run_id": "r1"}))
        assert resp.status_code == 202


# --------------------------------------------------------------------------
# Runtime registration / app integration
# --------------------------------------------------------------------------
class TestRuntimeRegistration:
    def test_create_durable_function_app_returns_dfapp(self) -> None:
        app = create_durable_function_app(func.AuthLevel.FUNCTION)
        assert type(app).__name__ == "DFApp"

    def test_register_durable_runtime_registers_all(self) -> None:
        app = create_durable_function_app(func.AuthLevel.FUNCTION)
        register_durable_runtime(
            app, DurableRuntimeDeps(registry={}, thread_lock=InProcessThreadLock())
        )
        names = {f.get_function_name() for f in app.get_functions()}
        assert names == {
            ORCHESTRATOR_NAME,
            "execute_langgraph_run",
            "aflg_durable_create_run",
            "aflg_durable_get_run",
            "aflg_durable_cancel_run",
        }

    async def test_registered_activity_executes_graph(self) -> None:
        """The registered activity closure rehydrates payload and runs the graph."""
        graph = FakeGraph(result={"z": 9})
        reg = FakeReg(graph, "echo")
        app = create_durable_function_app(func.AuthLevel.FUNCTION)
        register_durable_runtime(
            app, DurableRuntimeDeps(registry={"echo": reg}, thread_lock=InProcessThreadLock())
        )
        activity = next(
            f for f in app.get_functions() if f.get_function_name() == "execute_langgraph_run"
        )
        fn = activity.get_user_function()
        result = await fn(_payload(graph_name="echo").to_dict())
        assert result["activity_status"] == "success"
        assert result["result"] == {"z": 9}

    def test_registered_orchestrator_is_registered(self) -> None:
        app = create_durable_function_app(func.AuthLevel.FUNCTION)
        register_durable_runtime(
            app, DurableRuntimeDeps(registry={}, thread_lock=InProcessThreadLock())
        )
        orch = next(f for f in app.get_functions() if f.get_function_name() == ORCHESTRATOR_NAME)
        assert orch.get_user_function() is not None


class TestAppIntegration:
    def test_durable_app_wires_lifecycle_routes(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION, async_runs="durable")
        app.register(graph=FakeGraph(), name="echo")
        fa = app.function_app
        assert type(fa).__name__ == "DFApp"
        names = {f.get_function_name() for f in fa.get_functions()}
        assert {"aflg_durable_create_run", ORCHESTRATOR_NAME, "execute_langgraph_run"} <= names

    def test_default_app_is_plain_function_app(self) -> None:
        app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
        app.register(graph=FakeGraph(), name="echo")
        fa = app.function_app
        assert type(fa).__name__ == "FunctionApp"
        names = {f.get_function_name() for f in fa.get_functions()}
        assert not any(n and n.startswith("aflg_durable_") for n in names)
