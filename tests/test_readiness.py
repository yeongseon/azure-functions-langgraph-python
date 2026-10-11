from __future__ import annotations

import json
import time
from typing import Any

import azure.functions as func

from azure_functions_langgraph import LangGraphApp


def _handler(app: LangGraphApp) -> Any:
    function_app = app.function_app
    function_app.functions_bindings = {}
    for function in function_app.get_functions():
        if function.get_function_name() == "aflg_health_ready":
            return function.get_user_function()
    raise AssertionError("readiness route not registered")


def _request() -> func.HttpRequest:
    return func.HttpRequest(method="GET", url="/api/health/ready", body=b"")


def test_readiness_route_is_opt_in() -> None:
    names = {
        function.get_function_name() for function in LangGraphApp().function_app.get_functions()
    }
    assert "aflg_health_ready" not in names


def test_readiness_reports_sanitized_components() -> None:
    app = LangGraphApp(readiness_checks={"table": lambda: True, "model": lambda: False})
    response = _handler(app)(_request())
    assert response.status_code == 503
    assert json.loads(response.get_body()) == {
        "status": "unready",
        "components": {"table": "ok", "model": "failed"},
    }


def test_readiness_timeout_is_sanitized() -> None:
    def slow() -> bool:
        time.sleep(0.02)
        return True

    app = LangGraphApp(readiness_checks={"slow": slow}, readiness_timeout_seconds=0.001)
    response = _handler(app)(_request())
    assert response.status_code == 503
    assert json.loads(response.get_body())["components"] == {"slow": "failed"}


def test_readiness_metadata_is_conditional() -> None:
    disabled = LangGraphApp().get_app_metadata()
    enabled = LangGraphApp(readiness_checks={"table": lambda: True}).get_app_metadata()
    assert all(route.path != "/api/health/ready" for route in disabled.app_routes)
    assert any(route.path == "/api/health/ready" for route in enabled.app_routes)
