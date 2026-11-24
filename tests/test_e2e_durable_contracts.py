"""Offline checks of the Durable e2e request/response contract against the real HTTP handlers."""

from __future__ import annotations

import json
from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph.durable._runtime import create_run_http, get_run_http
from tests.e2e_durable._contracts import (
    PUBLIC_RUN_ERROR,
    check_create_response,
    check_public_error,
    check_success_evidence,
    create_body,
)


class _Status:
    def __init__(self, runtime_status: str, output: Any = None) -> None:
        self.runtime_status = runtime_status
        self.output = output


class _Client:
    def __init__(self, status: _Status | None = None) -> None:
        self._status = status

    async def start_new(
        self, name: str, instance_id: str | None = None, client_input: Any = None
    ) -> str:
        return instance_id or ""

    async def get_status(self, instance_id: str) -> _Status | None:
        return self._status

    async def terminate(self, instance_id: str, reason: str) -> None:
        return None


class _Reg:
    name = "e2e_durable"


def _req(route_params: dict[str, str], body: bytes = b"", method: str = "POST") -> func.HttpRequest:
    return func.HttpRequest(method=method, url="/api/x", body=body, route_params=route_params)


def test_create_body_defaults_unique_thread_id_and_keeps_explicit() -> None:
    a, b = create_body("run-a"), create_body("run-b")
    assert a["thread_id"] == "t-run-a" and b["thread_id"] == "t-run-b"
    assert create_body("run-a", thread_id="shared")["thread_id"] == "shared"
    assert a["input"] == {"run_id": "run-a", "hold_seconds": 0, "fail": False}


async def test_real_create_handler_response_satisfies_contract_with_request_id() -> None:
    body = create_body("run-1", hold=3)
    resp = await create_run_http(
        _Client(),
        _req({"name": "e2e_durable"}, json.dumps(body).encode()),
        registry={"e2e_durable": _Reg()},  # type: ignore[dict-item]
    )
    assert resp.status_code == 202
    payload = json.loads(resp.get_body())
    assert "request_id" in payload  # the exact-dict assertion would have failed here
    check_create_response(payload, "run-1")


def test_create_contract_rejects_wrong_status_or_id() -> None:
    with pytest.raises(AssertionError):
        check_create_response({"run_id": "r", "status": "running"}, "r")
    with pytest.raises(AssertionError):
        check_create_response({"run_id": "x", "status": "pending"}, "r")


async def test_real_get_handler_sanitizes_failed_activity_error() -> None:
    raw = {"activity_status": "error", "error": {"type": "RuntimeError", "message": "secret"}}
    resp = await get_run_http(
        _Client(_Status("Completed", raw)), _req({"run_id": "r"}, method="GET")
    )
    final = json.loads(resp.get_body())
    assert "type" not in final["output"]["error"]  # the old assertion raised KeyError
    assert final["output"]["error"] == PUBLIC_RUN_ERROR
    check_public_error(final)


def test_public_error_contract_fails_on_unsanitized_or_non_error() -> None:
    with pytest.raises(AssertionError):
        check_public_error(
            {
                "status": "error",
                "output": {"activity_status": "error", "error": {"type": "RuntimeError"}},
            }
        )
    with pytest.raises(AssertionError):
        check_public_error({"status": "success", "output": {"activity_status": "success"}})


def test_success_evidence_allows_redelivery_but_requires_one_applied_effect() -> None:
    assert check_success_evidence({"attempts": ["a"], "effects_applied": 1}) == 1
    assert check_success_evidence({"attempts": ["a", "b"], "effects_applied": 1}) == 2
    with pytest.raises(AssertionError):
        check_success_evidence({"attempts": [], "effects_applied": 1})
    with pytest.raises(AssertionError):
        check_success_evidence({"attempts": ["a"], "effects_applied": 2})
