from __future__ import annotations

from collections.abc import Callable
import json
import math
import threading
import time
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp


class _ReadinessFailure(RuntimeError):
    pass


def _handler(app: LangGraphApp) -> Callable[[func.HttpRequest], func.HttpResponse]:
    function_app = app.function_app
    function_app.functions_bindings = {}
    for function in function_app.get_functions():
        if function.get_function_name() == "aflg_health_ready":
            return function.get_user_function()
    raise AssertionError("readiness route not registered")


def _request() -> func.HttpRequest:
    return func.HttpRequest(method="GET", url="/api/health/ready", body=b"")


def _body(response: func.HttpResponse) -> Any:
    return json.loads(response.get_body())


def test_readiness_route_is_opt_in() -> None:
    names = {
        function.get_function_name() for function in LangGraphApp().function_app.get_functions()
    }

    assert "aflg_health_ready" not in names


def test_readiness_reports_mixed_sync_results() -> None:
    app = LangGraphApp(readiness_checks={"table": lambda: True, "model": lambda: False})

    response = _handler(app)(_request())

    assert response.status_code == 503
    assert _body(response) == {
        "status": "unready",
        "components": {"table": "ok", "model": "failed"},
    }


def test_readiness_sanitizes_sync_exception() -> None:
    secret = "https://user:password@example.invalid"

    def broken() -> bool:
        raise _ReadinessFailure(secret)

    app = LangGraphApp(readiness_checks={"database": broken})

    response = _handler(app)(_request())

    assert response.status_code == 503
    assert _body(response) == {
        "status": "unready",
        "components": {"database": "failed"},
    }
    assert secret.encode() not in response.get_body()


@pytest.mark.parametrize(("result", "expected_status"), [(True, 200), (False, 503)])
def test_readiness_awaits_async_result(result: bool, expected_status: int) -> None:
    executed = False

    async def check() -> bool:
        nonlocal executed
        executed = True
        return result

    app = LangGraphApp(readiness_checks={"async": check})

    response = _handler(app)(_request())

    assert response.status_code == expected_status
    assert executed is True
    assert _body(response)["components"] == {
        "async": "ok" if result else "failed",
    }


def test_readiness_sanitizes_async_exception() -> None:
    async def broken() -> bool:
        raise _ReadinessFailure("credential=top-secret")

    app = LangGraphApp(readiness_checks={"async": broken})

    response = _handler(app)(_request())

    assert response.status_code == 503
    assert _body(response) == {
        "status": "unready",
        "components": {"async": "failed"},
    }
    assert b"top-secret" not in response.get_body()


def test_readiness_timeout_bounds_response_before_callback_release() -> None:
    release = threading.Event()
    started = threading.Event()
    completed = threading.Event()
    responses: list[func.HttpResponse] = []

    def blocked() -> bool:
        started.set()
        release.wait()
        return True

    def invoke() -> None:
        responses.append(_handler(app)(_request()))
        completed.set()

    app = LangGraphApp(readiness_checks={"blocked": blocked}, readiness_timeout_seconds=0.02)
    probe = threading.Thread(target=invoke)
    try:
        before = time.monotonic()

        probe.start()

        assert started.wait(timeout=0.1)
        assert completed.wait(timeout=0.15)
        assert time.monotonic() - before < 0.2
        assert responses[0].status_code == 503
        assert _body(responses[0])["components"] == {"blocked": "failed"}
    finally:
        release.set()
        probe.join(timeout=0.5)


def test_repeated_timeouts_reuse_in_flight_callback() -> None:
    release = threading.Event()
    started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked() -> bool:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        release.wait()
        return True

    app = LangGraphApp(readiness_checks={"blocked": blocked}, readiness_timeout_seconds=0.005)
    handler = _handler(app)
    try:
        responses = [handler(_request()) for _ in range(8)]

        assert started.wait(timeout=0.1)
        assert calls == 1
        assert all(response.status_code == 503 for response in responses)
    finally:
        release.set()


def test_readiness_total_deadline_applies_to_mixed_callbacks() -> None:
    release = threading.Event()

    async def healthy() -> bool:
        return True

    def blocked() -> bool:
        release.wait()
        return True

    app = LangGraphApp(
        readiness_checks={"sync": lambda: False, "async": healthy, "blocked": blocked},
        readiness_timeout_seconds=0.02,
    )
    try:
        response = _handler(app)(_request())

        assert response.status_code == 503
        assert _body(response) == {
            "status": "unready",
            "components": {"sync": "failed", "async": "ok", "blocked": "failed"},
        }
    finally:
        release.set()


@pytest.mark.parametrize("timeout", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_readiness_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="readiness_timeout_seconds"):
        LangGraphApp(readiness_timeout_seconds=timeout)


def test_readiness_metadata_is_conditional() -> None:
    disabled = LangGraphApp().get_app_metadata()
    enabled = LangGraphApp(readiness_checks={"table": lambda: True}).get_app_metadata()

    assert all(route.path != "/api/health/ready" for route in disabled.app_routes)
    assert any(route.path == "/api/health/ready" for route in enabled.app_routes)
