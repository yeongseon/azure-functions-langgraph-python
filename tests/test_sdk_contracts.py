from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
import json


def _contracts() -> object:
    return import_module("azure_functions_langgraph.platform.contracts")


def _sse() -> object:
    return import_module("azure_functions_langgraph.platform._sse")


def _routes() -> object:
    return import_module("azure_functions_langgraph.platform.routes")


def test_assistant_response_has_required_fields() -> None:
    required_fields = {
        "assistant_id",
        "graph_id",
        "config",
        "created_at",
        "metadata",
        "version",
        "name",
        "description",
        "updated_at",
    }
    assistant = getattr(_contracts(), "Assistant")
    model_fields = set(assistant.model_fields.keys())
    assert required_fields.issubset(model_fields), f"Missing: {required_fields - model_fields}"


def test_thread_response_has_required_fields() -> None:
    required_fields = {
        "thread_id",
        "created_at",
        "updated_at",
        "metadata",
        "status",
        "values",
    }
    thread = getattr(_contracts(), "Thread")
    model_fields = set(thread.model_fields.keys())
    assert required_fields.issubset(model_fields), f"Missing: {required_fields - model_fields}"


def test_thread_state_response_has_required_fields() -> None:
    required_fields = {
        "values",
        "next",
        "checkpoint",
        "metadata",
        "created_at",
        "parent_checkpoint",
        "tasks",
        "interrupts",
    }
    thread_state = getattr(_contracts(), "ThreadState")
    model_fields = set(thread_state.model_fields.keys())
    assert required_fields.issubset(model_fields)


def test_checkpoint_has_required_fields() -> None:
    required_fields = {"thread_id", "checkpoint_ns", "checkpoint_id"}
    checkpoint = getattr(_contracts(), "Checkpoint")
    model_fields = set(checkpoint.model_fields.keys())
    assert required_fields.issubset(model_fields)


def test_run_create_has_required_fields() -> None:
    required_fields = {"assistant_id", "input", "config", "stream_mode"}
    run_create = getattr(_contracts(), "RunCreate")
    model_fields = set(run_create.model_fields.keys())
    assert required_fields.issubset(model_fields)


def test_error_response_format() -> None:
    platform_error = getattr(_routes(), "_platform_error")
    resp = platform_error(404, "not found")
    body = json.loads(resp.get_body())
    assert "detail" in body
    assert body["detail"] == "not found"
    assert resp.status_code == 404


def test_sse_metadata_event_format() -> None:
    format_metadata_event = getattr(_sse(), "format_metadata_event")
    event = format_metadata_event("run-123")
    assert event.startswith("event: metadata\n")
    assert "run-123" in event
    assert event.endswith("\n\n")


def test_sse_data_event_format() -> None:
    format_data_event = getattr(_sse(), "format_data_event")
    event = format_data_event("values", {"key": "val"})
    assert event.startswith("event: values\n")
    assert event.endswith("\n\n")


def test_sse_end_event_format() -> None:
    format_end_event = getattr(_sse(), "format_end_event")
    event = format_end_event()
    assert "event: end" in event


def test_sse_error_event_format() -> None:
    format_error_event = getattr(_sse(), "format_error_event")
    event = format_error_event("something broke")
    assert "event: error" in event
    assert "something broke" in event


def test_run_create_ignores_extra_fields() -> None:
    data = {"assistant_id": "test", "unknown_future_field": True}
    run_create = getattr(_contracts(), "RunCreate")
    model = run_create.model_validate(data)
    assert model.assistant_id == "test"


def test_thread_create_ignores_extra_fields() -> None:
    data = {"metadata": {}, "unknown_future_field": True}
    thread_create = getattr(_contracts(), "ThreadCreate")
    model = thread_create.model_validate(data)
    assert model.metadata == {}


def test_assistant_json_serialization_roundtrip() -> None:
    assistant_model = getattr(_contracts(), "Assistant")
    assistant = assistant_model(
        assistant_id="test",
        graph_id="test",
        config={},
        created_at=datetime.now(timezone.utc),
        metadata=None,
        version=1,
        name="test",
        description=None,
        updated_at=datetime.now(timezone.utc),
    )
    dumped = assistant.model_dump(mode="json")
    assert isinstance(dumped["assistant_id"], str)
    assert isinstance(dumped["version"], int)
    assert isinstance(dumped["config"], dict)
