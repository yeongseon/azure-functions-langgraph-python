"""Failed backend output is not public application result data."""

import json
import logging

import pytest

from azure_functions_langgraph.durable._runtime import get_run_http
from tests.test_durable import FakeDurableClient, FakeStatus, _req


@pytest.mark.parametrize("runtime", ["Failed", "Completed"])
@pytest.mark.parametrize(
    "output",
    [
        "private-backend-detail",
        {"arbitrary": "private-backend-detail"},
        {"error": {"type": "InternalError", "message": "private-backend-detail"}},
        {"error": "private-backend-detail", "diagnostics": {"trace": "private-trace"}},
        ["private-backend-detail"],
        None,
        object(),
    ],
)
async def test_failed_output_is_fixed_when_backend_output_is_untrusted(
    runtime: str, output: object, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: Completed represents a normalized Activity failure; Failed may be opaque.
    if runtime == "Completed":
        output = {"activity_status": "error", "diagnostics": output}
    client = FakeDurableClient(FakeStatus(runtime, output))

    # When
    with caplog.at_level(logging.ERROR):
        response = await get_run_http(client, _req(route_params={"run_id": "r1"}, method="GET"))

    # Then: no backend field is copied, even when it cannot be JSON-serialized.
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {
        "run_id": "r1",
        "status": "error",
        "output": {
            "activity_status": "error",
            "error": {"code": "run_failed", "message": "The run failed."},
        },
    }
    assert caplog.records


@pytest.mark.parametrize("output", [{"activity_status": "success", "result": {"y": 1}}, "ok"])
async def test_success_output_is_preserved_when_backend_completes(output: object) -> None:
    # Given
    client = FakeDurableClient(FakeStatus("Completed", output))
    # When
    response = await get_run_http(client, _req(route_params={"run_id": "r1"}, method="GET"))
    # Then
    assert json.loads(response.get_body()) == {
        "run_id": "r1",
        "status": "success",
        "output": output,
    }
