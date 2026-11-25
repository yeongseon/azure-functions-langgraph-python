"""Existing-app composition uses the same native route contract as standalone."""

from __future__ import annotations

import json

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from tests.conftest import FakeCompiledGraph, FakeStatefulGraph


def test_blueprint_preserves_existing_triggers() -> None:
    # Given an already populated application and a protocol-based graph.
    existing = func.FunctionApp()

    @existing.route(route="existing", methods=["GET"])
    def existing_http(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse("existing")

    @existing.timer_trigger(schedule="0 */5 * * * *", arg_name="timer")
    def existing_timer(timer: func.TimerRequest) -> None:
        pass

    adapter = LangGraphApp()
    adapter.register(
        FakeCompiledGraph(invoke_result={"message": "hello", "processed": True}), "agent"
    )

    # When the native blueprint is registered through the supported SDK API.
    existing.register_functions(adapter.to_blueprint())

    # Then both old triggers survive and the new graph is callable.
    functions = {f.get_function_name(): f for f in existing.get_functions()}
    request = func.HttpRequest("POST", "/", body=b'{"input":{"message":"hello"}}')
    assert functions["existing_http"].get_user_function()(request).get_body() == b"existing"
    assert functions["existing_timer"].get_trigger().get_dict_repr()["type"] == "timerTrigger"
    response = functions["aflg_agent_invoke"].get_user_function()(request)
    assert response.status_code == 200
    assert json.loads(response.get_body())["output"] == {"message": "hello", "processed": True}


@pytest.mark.parametrize("stream", [True, False])
def test_blueprint_matches_standalone_routes_and_auth(stream: bool) -> None:
    # Given explicit adapter auth different from the existing app's default.
    adapter = LangGraphApp(readiness_checks={"dependency": lambda: True})
    adapter.register(FakeStatefulGraph(), "secure", stream=stream, auth_level=func.AuthLevel.ADMIN)
    adapter.register(FakeCompiledGraph(), "default")
    standalone = adapter.function_app
    existing = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

    # When exporting the same registrations for composition.
    existing.register_functions(adapter.to_blueprint())

    # Then route names, bindings, methods and explicit auth are identical.
    assert {f.get_function_name(): f.get_dict_repr() for f in existing.get_functions()} == {
        f.get_function_name(): f.get_dict_repr() for f in standalone.get_functions()
    }
    assert adapter.function_app is standalone


def test_blueprint_registration_is_a_route_snapshot() -> None:
    # Given an exported blueprint before another graph is registered.
    adapter = LangGraphApp()
    adapter.register(FakeCompiledGraph(), "first")
    blueprint = adapter.to_blueprint()
    existing = func.FunctionApp()
    existing.register_functions(blueprint)

    # When another graph is added later.
    adapter.register(FakeCompiledGraph(), "later")

    # Then the existing app is not retroactively populated with new routes.
    assert {f.get_function_name() for f in existing.get_functions()} == {
        "aflg_health",
        "aflg_health_details",
        "aflg_first_invoke",
        "aflg_first_stream",
    }
    assert "aflg_later_invoke" in {
        f.get_function_name() for f in adapter.function_app.get_functions()
    }


def test_blueprint_rechecks_topology_at_export(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a valid local app whose deployment settings later become unsafe.
    adapter = LangGraphApp()
    adapter.register(FakeCompiledGraph(), "agent")
    monkeypatch.setenv("AZFUNC_LANGGRAPH_LOCK_BACKEND", "distributed")

    # When exporting, then the same topology guard refuses the local lock.
    with pytest.raises(RuntimeError, match="distributed"):
        adapter.to_blueprint()


@pytest.mark.parametrize("surface", ["platform", "durable", "service_bus"])
def test_blueprint_rejects_optional_surfaces(surface: str) -> None:
    # Given an optional transport outside native blueprint support.
    adapter = LangGraphApp(platform_compat=surface == "platform")
    if surface == "durable":
        adapter.async_runs = "durable"
    if surface == "service_bus":
        adapter.register_service_bus(FakeCompiledGraph(), "agent", connection="BUS", queue_name="q")

    # When exporting, then optional routes are not silently dropped.
    with pytest.raises(ValueError, match="native HTTP"):
        adapter.to_blueprint()
