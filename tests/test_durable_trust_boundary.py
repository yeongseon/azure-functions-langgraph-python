"""Pin #984's current app-wide boundary, not a future authorization policy.

Invoke registered handlers with in-process clients: this does not exercise host
key enforcement or prove that a multi-principal deployment is safe.
"""

from __future__ import annotations

from inspect import unwrap
import json

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import AuthorizationContext
from tests.test_durable import FakeDurableClient, FakeGraph, FakeStatus, _Enum


@pytest.mark.parametrize(
    ("handler_name", "method", "path", "expected_status", "expected_calls"),
    [
        ("aflg_durable_create_run", "POST", "graphs/agent/runs", 202, (1, 0)),
        ("aflg_durable_get_run", "GET", "runs/r1", 200, (0, 0)),
        ("aflg_durable_cancel_run", "POST", "runs/r1/cancel", 202, (0, 1)),
    ],
)
async def test_registered_durable_routes_bypass_native_authorizer(
    handler_name: str,
    method: str,
    path: str,
    expected_status: int,
    expected_calls: tuple[int, int],
) -> None:
    # Given: a native policy denying every caller, and a known in-flight run.
    pytest.importorskip("azure.durable_functions")
    seen: list[AuthorizationContext] = []

    def deny(context: AuthorizationContext) -> bool:
        seen.append(context)
        return False

    app = LangGraphApp(async_runs="durable", authorize=deny)
    app.register(FakeGraph(), name="agent")
    handlers = {
        f.get_function_name(): f.get_user_function() for f in app.function_app.get_functions()
    }
    client = FakeDurableClient(status=FakeStatus(_Enum("Running")))
    request = func.HttpRequest(
        method=method,
        url=f"http://localhost/api/{path}",
        route_params={"name": "agent", "run_id": "r1"},
        body=b'{"run_id": "r1", "input": {}}',
    )

    # When: the registered app handler runs with an already-injected client.
    response = await unwrap(handlers[handler_name])(request, client)

    # Then: native denial does not protect the lifecycle or its output.
    assert response.status_code == expected_status
    assert json.loads(response.get_body())["run_id"] == "r1"
    assert (len(client.started), len(client.terminated)) == expected_calls
    assert seen == []
