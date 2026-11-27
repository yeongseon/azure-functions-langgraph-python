"""Composition boundaries use real SDK builders, not a host or cloud backend."""

from __future__ import annotations

from collections.abc import Callable
import json

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.durable._runtime import (
    DurableRuntimeDeps,
    create_durable_function_app,
    register_durable_runtime,
)
from azure_functions_langgraph.locks import InProcessThreadLock
from tests.conftest import FakeCompiledGraph


def _bindings(app: func.FunctionApp) -> list[str]:
    return [builder._function.get_function_json() for builder in app._function_builders]


def _durable_parent() -> func.FunctionApp:
    pytest.importorskip("azure.durable_functions")
    app = create_durable_function_app(func.AuthLevel.ANONYMOUS)
    register_durable_runtime(
        app, DurableRuntimeDeps(registry={}, thread_lock=InProcessThreadLock())
    )
    return app


@pytest.fixture(params=[func.FunctionApp, _durable_parent], ids=["FunctionApp", "DFApp"])
def parent(request: pytest.FixtureRequest) -> func.FunctionApp:
    factory: Callable[[], func.FunctionApp] = request.param
    app = factory()

    @app.route(route="existing", methods=["GET"], auth_level=func.AuthLevel.ADMIN)
    def existing_http(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse("existing")

    @app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer")
    def existing_timer(timer: func.TimerRequest) -> None:
        pass

    @app.service_bus_queue_trigger(arg_name="message", queue_name="existing", connection="BUS")
    def existing_bus(message: func.ServiceBusMessage) -> None:
        pass

    return app


@pytest.mark.parametrize(
    "modes",
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_parent_is_unchanged_when_optional_export_is_rejected(
    parent: func.FunctionApp, modes: tuple[bool, bool, bool]
) -> None:
    # Given native routes plus every non-empty combination of optional modes.
    platform, durable, bus = modes
    adapter = LangGraphApp(platform_compat=platform, async_runs="durable" if durable else False)
    graph = FakeCompiledGraph()
    adapter.register(graph, "agent")
    if bus:
        adapter.register_service_bus(graph, "agent", connection="BUS", queue_name="q")
    before = _bindings(parent)

    # When composing, export must fail before the SDK receives a child.
    with pytest.raises(ValueError, match="native HTTP"):
        parent.register_functions(adapter.to_blueprint())

    # Then no native subset was appended and every original binding survives.
    assert _bindings(parent) == before


@pytest.mark.parametrize("durable", [False, True], ids=["FunctionApp-child", "DFApp-child"])
def test_parent_is_unchanged_when_standalone_app_is_used_as_child(
    parent: func.FunctionApp, durable: bool
) -> None:
    # Given standalone app export, which is not a Blueprint composition API.
    if durable:
        pytest.importorskip("azure.durable_functions")
    child = LangGraphApp(platform_compat=True, async_runs="durable" if durable else False)
    before = _bindings(parent)

    # When bypassing Blueprint export, the SDK rejects the standalone child.
    with pytest.raises(TypeError, match="FunctionRegister"):
        parent.register_functions(child.function_app)

    # Then the parent has not received any of the optional or native builders.
    assert _bindings(parent) == before


@pytest.mark.parametrize("topic", [False, True], ids=["queue", "topic"])
@pytest.mark.parametrize("session", [False, True], ids=["ordinary", "session"])
def test_parent_is_unchanged_when_service_bus_export_is_rejected(
    parent: func.FunctionApp, topic: bool, session: bool
) -> None:
    # Given a Service Bus-only adapter; absence of native graphs is not an exemption.
    adapter = LangGraphApp()
    adapter.register_service_bus(
        FakeCompiledGraph(),
        "agent",
        connection="BUS",
        queue_name=None if topic else "q",
        topic_name="topic" if topic else None,
        subscription_name="subscription" if topic else None,
        session=session,
        session_exclusive=session,
    )
    before = _bindings(parent)

    # When composing, all Service Bus binding variants are outside export support.
    with pytest.raises(ValueError, match="native HTTP"):
        parent.register_functions(adapter.to_blueprint())

    # Then even session-exclusive export cannot partially populate the parent.
    assert _bindings(parent) == before


def test_native_export_preserves_non_native_parent_triggers(parent: func.FunctionApp) -> None:
    # Given an existing FunctionApp/DFApp with its own HTTP, timer and Bus triggers.
    before = _bindings(parent)
    adapter = LangGraphApp()
    adapter.register(FakeCompiledGraph(invoke_result={"answer": 42}), "agent")

    # When the adapter itself is native-only, the parent's integrations do not forbid it.
    parent.register_functions(adapter.to_blueprint())

    # Then original bindings remain intact and the attached graph executes.
    assert _bindings(parent)[: len(before)] == before
    functions = {f.get_function_name(): f for f in parent.get_functions()}
    response = functions["aflg_agent_invoke"].get_user_function()(
        func.HttpRequest("POST", "/", body=b'{"input":{}}')
    )
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"output": {"answer": 42}}


def test_function_name_collision_fails_after_registration(parent: func.FunctionApp) -> None:
    # Given a parent function whose name, but not path, collides with adapter health.
    @parent.function_name(name="aflg_health")
    @parent.route(route="custom-health", methods=["GET"])
    def custom_health(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse("custom")

    adapter = LangGraphApp()
    before = len(parent._function_builders)

    # When registering, SDK append succeeds; duplicate names fail only at indexing.
    parent.register_functions(adapter.to_blueprint())

    # Then indexing fails without rolling back the two appended health builders.
    with pytest.raises(ValueError, match="aflg_health"):
        parent.get_functions()
    assert len(parent._function_builders) == before + 2


def test_route_collision_is_not_a_function_name_collision(parent: func.FunctionApp) -> None:
    # Given distinct function names sharing the same HTTP method and path.
    @parent.route(route="health", methods=["GET"])
    def custom_health(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse("custom")

    # When composing, neither export nor SDK function-name validation detects this.
    parent.register_functions(LangGraphApp().to_blueprint())

    # Then both conflicting bindings exist; this does not certify host routing.
    functions = {f.get_function_name(): f for f in parent.get_functions()}
    for name in ("custom_health", "aflg_health"):
        trigger = functions[name].get_trigger()
        assert trigger is not None
        binding = trigger.get_dict_repr()
        assert binding["route"] == "health"
        assert binding["methods"] == [func.HttpMethod.GET]
