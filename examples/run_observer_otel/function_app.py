"""OTel run-observer example - Azure Functions entry point.

Demonstrates the optional OpenTelemetry observer (issue #434): an
:class:`OTelRunObserver` is registered on ``LangGraphApp`` and maps every native
invoke/stream run lifecycle event (started / completed / failed / rejected) to
an OpenTelemetry span carrying only **safe correlation fields** — never input,
output, config, or secrets.

Boundary: the package owns the domain signal only. It does **not** configure a
``TracerProvider`` or exporters — the operator owns all SDK/exporter wiring. In
Azure Functions, the Application Insights integration (or the OpenTelemetry
distro / an OTLP exporter you configure) collects the spans. This example wires
a console exporter locally so ``func start`` prints the spans to the host log.

Run from this directory:
    pip install "azure-functions-langgraph[otel]" opentelemetry-sdk "langgraph>=1.1"
    func start
"""

from __future__ import annotations

from graph import compiled_graph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.observability_otel import OTelRunObserver


def _configure_local_tracer() -> None:
    """Wire a console span exporter for local `func start` demos.

    This is **operator-owned** SDK configuration living in the example, not in
    the package. In production, replace it with your Application Insights /
    OpenTelemetry distro / OTLP exporter setup. If the OTel SDK is not
    installed, the observer still works — spans are simply dropped by the
    default no-op provider.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            ConsoleSpanExporter,
            SimpleSpanProcessor,
        )
    except ImportError:
        return

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return  # A provider is already installed; don't override the operator's.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


_configure_local_tracer()

langgraph_app = LangGraphApp(observer=OTelRunObserver())
langgraph_app.register(
    graph=compiled_graph,
    name="run_observer_otel",
    description="A deterministic agent with an OpenTelemetry RunObserver (issue #434)",
)

app = langgraph_app.function_app
