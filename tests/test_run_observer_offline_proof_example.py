"""Offline proof that the observer's run telemetry carries no request payloads.

Drives ``examples/run_observer_offline_proof`` (real registered handler, built-in
``LoggingRunObserver``, fixed fake request IDs, no network or credentials).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType

import pytest

from azure_functions_langgraph.observability import RunContext, RunRejectedReason
from tests.example_imports import load_example_file

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "run_observer_offline_proof"


@pytest.fixture()
def proof() -> ModuleType:
    return load_example_file(EXAMPLE_DIR / "proof.py", "_examples_run_observer_offline_proof")


def test_lifecycle_events_carry_safe_correlation_fields(proof: ModuleType) -> None:
    result = proof.run_proof()
    fields = [r.langgraph_run for r in result["records"]]

    assert [(f["run_id"], f["status"]) for f in fields] == [
        (proof.REQUEST_ID_OK, "started"),
        (proof.REQUEST_ID_OK, "completed"),
        (proof.REQUEST_ID_FAILED, "started"),
        (proof.REQUEST_ID_FAILED, "failed"),
        (proof.REQUEST_ID_REJECTED, "rejected"),
    ]
    for f in fields:
        assert f["graph_name"] == proof.GRAPH_NAME
        assert f["endpoint"] == "invoke"
    assert fields[1]["thread_id"] == proof.THREAD_ID
    assert fields[3]["error_type"] == "RuntimeError"
    assert fields[4]["reason"] == "invalid_request"
    assert fields[4]["thread_id"] is None
    assert result["responses"]["success"].headers["X-Request-ID"] == proof.REQUEST_ID_OK


def test_canary_flows_through_app_but_not_observer_output(proof: ModuleType) -> None:
    result = proof.run_proof()

    # Non-vacuous: the canary really reached the graph and came back in the response.
    assert proof.CANARY in result["responses"]["success"].get_body().decode()
    # The failing node's exception message embeds the canary, yet only its class name is logged.
    assert result["responses"]["failure"].status_code >= 500
    output = proof.render(result)
    assert proof.CANARY not in output
    assert "echo node failed" not in output
    for record in result["all_records"]:
        assert proof.CANARY not in repr(record.__dict__)
    proof.assert_payload_free(result)


class _LeakyObserver:
    """Mutation: an observer that (wrongly) logs the exception message."""

    def __init__(self) -> None:
        self._log = logging.getLogger(proof_logger_name())

    def on_run_started(self, ctx: RunContext) -> None: ...

    def on_run_completed(self, ctx: RunContext) -> None: ...

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self._log.error("run failed: %s", exc, extra={"langgraph_run": {"run_id": ctx.run_id}})

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None: ...


def proof_logger_name() -> str:
    return "run_observer_offline_proof.observer"


def test_canary_check_fails_for_a_leaky_observer(proof: ModuleType) -> None:
    result = proof.run_proof(observer=_LeakyObserver())

    with pytest.raises(AssertionError, match="payload leaked"):
        proof.assert_payload_free(result)
