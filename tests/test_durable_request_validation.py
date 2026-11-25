import json

import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.durable._runtime import create_run_http
from tests.test_durable import FakeDurableClient, FakeGraph, FakeReg, _req


@pytest.mark.parametrize(
    "body",
    [
        {"config": 42},
        {"config": False},
        {"config": "private-input"},
        {"config": []},
        {"config": [["key", "value"]]},
        *({"config": {"configurable": value}} for value in [42, False, [], "private-input"]),
        *({"thread_id": value} for value in [42, False, [], {}, "", "bad\n", "é", "x" * 257]),
        *(
            {"config": {"configurable": {"thread_id": value}}}
            for value in [42, False, [], {}, "", "bad\n", "é", "x" * 257]
        ),
    ],
)
async def test_create_rejects_invalid_fields_before_scheduling(body: dict[str, object]) -> None:
    # Given
    client = FakeDurableClient()
    # When
    response = await create_run_http(
        client,
        _req(route_params={"name": "echo"}, body=json.dumps(body).encode()),
        registry={"echo": FakeReg(FakeGraph(), "echo")},
    )
    # Then
    assert response.status_code == 400
    assert isinstance(json.loads(response.get_body())["error"], str)
    assert "private-input" not in response.get_body().decode()
    assert client.started == []


@pytest.mark.parametrize(
    "body,expected_thread",
    [
        ({}, None),
        ({"input": None, "config": None}, None),
        ({"input": [1, "two"], "config": {"configurable": None}}, None),
        ({"input": 1, "config": {"tags": ["legacy"]}}, None),
        ({"thread_id": "explicit", "config": {"configurable": {"thread_id": 42}}}, "explicit"),
        ({"thread_id": "explicit", "config": {"configurable": {"thread_id": "other"}}}, "explicit"),
        ({"thread_id": None, "config": {"configurable": {"thread_id": "derived"}}}, "derived"),
    ],
)
async def test_create_preserves_legacy_requests_and_thread_precedence(
    body: dict[str, object], expected_thread: str | None
) -> None:
    # Given
    client = FakeDurableClient()
    # When
    response = await create_run_http(
        client,
        _req(route_params={"name": "echo"}, body=json.dumps(body).encode()),
        registry={"echo": FakeReg(FakeGraph(), "echo")},
    )
    # Then
    assert response.status_code == 202
    assert len(client.started) == 1
    assert client.started[0][2]["thread_id"] == expected_thread
    assert client.started[0][2]["input"] == body.get("input")
    assert client.started[0][2]["config"] == (body.get("config") or {})


@pytest.mark.parametrize("field", ["input", "config"])
@pytest.mark.parametrize("limit", ["depth", "nodes"])
async def test_configured_limits_reject_durable_input_before_scheduling(
    field: str, limit: str
) -> None:
    # Given: use the registered route to cover LangGraphApp -> deps -> HTTP wiring.
    app = LangGraphApp(async_runs="durable", max_input_depth=2, max_input_nodes=2)
    app.register(FakeGraph(), "echo")
    route = getattr(
        next(
            f
            for f in app.function_app.get_functions()
            if f.get_function_name() == "aflg_durable_create_run"
        ).get_user_function(),
        "__wrapped__",
    )
    value = {"a": {"b": 1}} if limit == "depth" else {"a": 1, "b": 2, "c": 3}
    client = FakeDurableClient()
    # When
    response = await route(
        _req(route_params={"name": "echo"}, body=json.dumps({field: value}).encode()), client
    )
    # Then
    assert response.status_code == 400
    assert client.started == []


@pytest.mark.parametrize("raw,expected", [(b"x" * 51, 413), (b"{bad", 400), (b"[]", 400)])
async def test_create_rejects_bad_body_before_scheduling(raw: bytes, expected: int) -> None:
    # Given
    client = FakeDurableClient()
    # When
    response = await create_run_http(
        client,
        _req(route_params={"name": "echo"}, body=raw),
        registry={"echo": FakeReg(FakeGraph(), "echo")},
        max_request_body_bytes=50,
    )
    # Then
    assert response.status_code == expected
    assert client.started == []
