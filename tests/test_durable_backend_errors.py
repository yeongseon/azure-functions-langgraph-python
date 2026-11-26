import json
import logging

import pytest

from azure_functions_langgraph.durable._runtime import create_run_http, get_run_http
from tests.test_durable import FakeDurableClient, FakeGraph, FakeReg, _req


@pytest.mark.parametrize("supplied_id", [None, "known-run"])
async def test_create_reports_uncertain_submission_when_backend_raises(
    supplied_id: str | None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    client = FakeDurableClient()
    calls: list[str | None] = []

    async def fail_start(
        orchestration_function_name: str,
        instance_id: str | None = None,
        client_input: object = None,
    ) -> str:
        calls.append(instance_id)
        raise RuntimeError("private-backend-detail")

    monkeypatch.setattr(client, "start_new", fail_start)
    body = {"input": 1, **({"run_id": supplied_id} if supplied_id else {})}
    # When
    with caplog.at_level(logging.ERROR):
        response = await create_run_http(
            client,
            _req(route_params={"name": "echo"}, body=json.dumps(body).encode()),
            registry={"echo": FakeReg(FakeGraph(), "echo")},
        )
    # Then
    payload = json.loads(response.get_body())
    assert response.status_code == 502
    assert len(calls) == 1
    assert payload["run_id"] == calls[0]
    assert payload["run_id"]
    assert payload["error"] == {
        "code": "submission_unknown",
        "message": "Unable to confirm run submission. Poll using the run ID before resubmitting.",
    }
    assert "private-backend-detail" not in response.get_body().decode()
    assert "private-backend-detail" in caplog.text


async def test_poll_returns_sanitized_error_when_backend_raises(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    client = FakeDurableClient()
    calls: list[str] = []

    async def fail_status(instance_id: str) -> None:
        calls.append(instance_id)
        raise RuntimeError("private-backend-detail")

    monkeypatch.setattr(client, "get_status", fail_status)
    # When
    with caplog.at_level(logging.ERROR):
        response = await get_run_http(client, _req(route_params={"run_id": "r1"}, method="GET"))
    # Then
    assert response.status_code == 502
    assert calls == ["r1"]
    assert json.loads(response.get_body()) == {
        "run_id": "r1",
        "error": {"code": "status_unavailable", "message": "Unable to retrieve run status."},
    }
    assert "private-backend-detail" in caplog.text
