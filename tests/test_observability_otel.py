"""Tests for the optional OpenTelemetry RunObserver (issue #434)."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
import pytest

from azure_functions_langgraph.observability import (
    RunContext,
    RunObserver,
    finish_context,
    new_run_context,
)
from azure_functions_langgraph.observability_otel import (
    OTelRunObserver,
    _safe_attributes,
)


def _attrs(span: Any) -> dict[str, Any]:
    """Narrow a span's Optional attribute mapping for typed indexing."""
    attributes = span.attributes
    assert attributes is not None
    return dict(attributes)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    """A fresh in-memory exporter wired to its own provider/tracer.

    Uses an isolated ``TracerProvider`` (not the global one) so tests never
    depend on process-wide OTel state or leak spans between cases.
    """
    return InMemorySpanExporter()


@pytest.fixture
def observer(exporter: InMemorySpanExporter) -> OTelRunObserver:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    return OTelRunObserver(tracer=tracer)


def _started(graph_name: str = "agent", endpoint: str = "invoke", **kw: object) -> RunContext:
    ctx = new_run_context(graph_name, endpoint, **kw)  # type: ignore[arg-type]
    return ctx


class TestProtocolConformance:
    def test_satisfies_run_observer_protocol(self, observer: OTelRunObserver) -> None:
        assert isinstance(observer, RunObserver)

    def test_default_tracer_resolves_without_error(self) -> None:
        # No tracer passed → falls back to trace.get_tracer(); must not raise.
        obs = OTelRunObserver()
        assert isinstance(obs, RunObserver)


class TestSuccessLifecycle:
    def test_started_then_completed_emits_ok_span(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        ctx = _started(thread_id="t1", assistant_id="a1")
        observer.on_run_started(ctx)
        # Span not exported until it ends.
        assert exporter.get_finished_spans() == ()
        observer.on_run_completed(finish_context(ctx))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "langgraph.run.invoke"
        assert span.status.status_code == StatusCode.OK
        assert _attrs(span)["langgraph.graph_name"] == "agent"
        assert _attrs(span)["langgraph.thread_id"] == "t1"
        assert _attrs(span)["langgraph.assistant_id"] == "a1"
        assert _attrs(span)["langgraph.status"] == "completed"
        assert _attrs(span)["langgraph.duration_ms"] >= 0

    def test_stream_endpoint_names_span(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        ctx = _started(endpoint="stream", stream_mode=["values", "updates"])
        observer.on_run_started(ctx)
        observer.on_run_completed(finish_context(ctx))
        span = exporter.get_finished_spans()[0]
        assert span.name == "langgraph.run.stream"
        # Tuple stream_mode is coerced to a list for a valid OTel attribute.
        assert _attrs(span)["langgraph.stream_mode"] == ("values", "updates")


class TestFailureLifecycle:
    def test_started_then_failed_records_exception(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        ctx = _started()
        observer.on_run_started(ctx)
        observer.on_run_failed(finish_context(ctx), ValueError("boom"))

        span = exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert _attrs(span)["langgraph.status"] == "failed"
        assert _attrs(span)["langgraph.error_type"] == "ValueError"
        # The exception is recorded as a span event, class only — no payload.
        assert any(e.name == "exception" for e in span.events)


class TestRejectedLifecycle:
    def test_rejected_emits_self_contained_span(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        # Rejections never call on_run_started.
        ctx = finish_context(_started())
        observer.on_run_rejected(ctx, "invalid_request")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == StatusCode.ERROR
        assert _attrs(span)["langgraph.status"] == "rejected"
        assert _attrs(span)["langgraph.reject_reason"] == "invalid_request"


class TestEdgeCases:
    def test_terminal_without_started_is_ignored(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        # A terminal event for an unknown run_id must be a no-op, not a crash.
        observer.on_run_completed(finish_context(_started()))
        observer.on_run_failed(finish_context(_started()), RuntimeError("x"))
        assert exporter.get_finished_spans() == ()

    def test_concurrent_runs_tracked_independently(
        self, observer: OTelRunObserver, exporter: InMemorySpanExporter
    ) -> None:
        a = _started(graph_name="a")
        b = _started(graph_name="b")
        observer.on_run_started(a)
        observer.on_run_started(b)
        observer.on_run_completed(finish_context(b))
        observer.on_run_failed(finish_context(a), KeyError("k"))

        spans = {_attrs(s)["langgraph.graph_name"]: s for s in exporter.get_finished_spans()}
        assert spans["b"].status.status_code == StatusCode.OK
        assert spans["a"].status.status_code == StatusCode.ERROR

    def test_custom_span_name_prefix(self, exporter: InMemorySpanExporter) -> None:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        obs = OTelRunObserver(tracer=provider.get_tracer("t"), span_name_prefix="myagent")
        ctx = _started()
        obs.on_run_started(ctx)
        obs.on_run_completed(finish_context(ctx))
        assert exporter.get_finished_spans()[0].name == "myagent.invoke"


class TestSafeAttributes:
    def test_none_fields_are_dropped(self) -> None:
        ctx = _started()  # thread_id/assistant_id/etc default to None
        attrs = _safe_attributes(ctx)
        assert "langgraph.thread_id" not in attrs
        assert "langgraph.assistant_id" not in attrs
        # Always-present safe fields remain.
        assert attrs["langgraph.graph_name"] == "agent"
        assert attrs["langgraph.transport"] == "buffered"

    def test_no_payload_fields_leak(self) -> None:
        ctx = _started(thread_id="t", has_checkpointer=True, lock_backend="inprocess")
        attrs = _safe_attributes(ctx)
        # Only the known safe correlation keys — no input/output/config/headers.
        allowed_suffixes = {
            "graph_name",
            "endpoint",
            "run_id",
            "thread_id",
            "assistant_id",
            "invocation_id",
            "stream_mode",
            "transport",
            "has_checkpointer",
            "lock_backend",
        }
        for key in attrs:
            assert key.split(".", 1)[1] in allowed_suffixes


def test_missing_extra_raises_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accessing OTelRunObserver without opentelemetry names the 'otel' extra."""
    import builtins
    import importlib
    import sys

    import azure_functions_langgraph

    # Drop any cached observer module and force the otel import to fail.
    monkeypatch.delitem(sys.modules, "azure_functions_langgraph.observability_otel", raising=False)
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("no opentelemetry")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    importlib.reload(azure_functions_langgraph)
    try:
        with pytest.raises(ImportError, match=r"otel.*extra"):
            _ = azure_functions_langgraph.OTelRunObserver
    finally:
        monkeypatch.undo()
        importlib.reload(azure_functions_langgraph)
