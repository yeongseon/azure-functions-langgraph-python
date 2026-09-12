"""Unit tests for the pure durable state models and status mapping.

These tests import only :mod:`azure_functions_langgraph.durable._state`, which
has no ``azure.durable_functions`` dependency, so they run without the
``durable`` extra or any Azure host.
"""

from __future__ import annotations

import pytest

from azure_functions_langgraph.durable._state import (
    DurableRunPayload,
    DurableRunResult,
    normalize_status,
    serialize_error,
)


class TestSerializeError:
    def test_type_and_message_only(self) -> None:
        err = serialize_error(ValueError("boom"))
        assert err == {"type": "ValueError", "message": "boom"}

    def test_no_traceback_or_extra_keys(self) -> None:
        try:
            raise RuntimeError("bad")
        except RuntimeError as exc:
            err = serialize_error(exc)
        assert set(err) == {"type", "message"}
        assert err["type"] == "RuntimeError"


class TestDurableRunPayload:
    def test_round_trip(self) -> None:
        payload = DurableRunPayload(
            run_id="r1",
            graph_name="agent",
            input={"messages": []},
            thread_id="t1",
            config={"configurable": {"thread_id": "t1"}},
            assistant_id="a1",
            correlation_id="c1",
        )
        restored = DurableRunPayload.from_dict(payload.to_dict())
        assert restored == payload

    def test_from_dict_defaults(self) -> None:
        payload = DurableRunPayload.from_dict(
            {"run_id": "r1", "graph_name": "agent", "input": None}
        )
        assert payload.thread_id is None
        assert payload.config == {}
        assert payload.assistant_id is None
        assert payload.correlation_id is None

    def test_from_dict_coerces_ids_to_str(self) -> None:
        payload = DurableRunPayload.from_dict({"run_id": 123, "graph_name": 456, "input": "x"})
        assert payload.run_id == "123"
        assert payload.graph_name == "456"

    def test_from_dict_none_config_becomes_empty(self) -> None:
        payload = DurableRunPayload.from_dict(
            {"run_id": "r", "graph_name": "g", "input": 1, "config": None}
        )
        assert payload.config == {}


class TestDurableRunResult:
    def test_success_factory_and_round_trip(self) -> None:
        result = DurableRunResult.success("r1", {"answer": 42})
        assert result.activity_status == "success"
        assert result.result == {"answer": 42}
        assert result.error is None
        assert DurableRunResult.from_dict(result.to_dict()) == result

    def test_failure_factory(self) -> None:
        result = DurableRunResult.failure("r1", ValueError("nope"))
        assert result.activity_status == "error"
        assert result.result is None
        assert result.error == {"type": "ValueError", "message": "nope"}

    def test_rejected_factory(self) -> None:
        result = DurableRunResult.rejected("r1", "thread busy")
        assert result.activity_status == "rejected"
        assert result.error == {
            "type": "ThreadContentionError",
            "message": "thread busy",
        }

    def test_from_dict_round_trip_error(self) -> None:
        result = DurableRunResult.failure("r1", RuntimeError("x"))
        assert DurableRunResult.from_dict(result.to_dict()) == result


class TestNormalizeStatus:
    @pytest.mark.parametrize(
        ("runtime_status", "expected"),
        [
            ("Pending", "pending"),
            ("Running", "running"),
            ("Suspended", "running"),
            ("ContinuedAsNew", "running"),
            ("Failed", "error"),
            ("Terminated", "cancelled"),
            ("Canceled", "cancelled"),
            ("Cancelled", "cancelled"),
        ],
    )
    def test_simple_statuses(self, runtime_status: str, expected: str) -> None:
        assert normalize_status(runtime_status) == expected

    def test_none_and_empty_are_running(self) -> None:
        assert normalize_status(None) == "running"
        assert normalize_status("") == "running"

    def test_unknown_status_is_running(self) -> None:
        assert normalize_status("SomethingNew") == "running"

    def test_completed_without_output_is_success(self) -> None:
        assert normalize_status("Completed") == "success"

    def test_completed_with_success_output(self) -> None:
        output = DurableRunResult.success("r1", {"x": 1}).to_dict()
        assert normalize_status("Completed", output) == "success"

    def test_completed_with_error_output_is_error(self) -> None:
        output = DurableRunResult.failure("r1", ValueError("boom")).to_dict()
        assert normalize_status("Completed", output) == "error"

    def test_completed_with_rejected_output_is_error(self) -> None:
        output = DurableRunResult.rejected("r1", "busy").to_dict()
        assert normalize_status("Completed", output) == "error"

    def test_whitespace_is_stripped(self) -> None:
        assert normalize_status("  Completed  ") == "success"
