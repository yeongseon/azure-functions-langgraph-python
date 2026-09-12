"""Durable Functions runtime wiring for the async-run control plane.

This is the only module in the ``durable`` package that touches the
``azure.durable_functions`` SDK, so it is imported lazily (behind the optional
``durable`` extra). It:

* constructs a ``df.DFApp`` (an :class:`azure.functions.FunctionApp`-compatible
  app that also understands orchestration/activity/durable-client bindings),
* registers the deterministic orchestrator
  (:func:`~azure_functions_langgraph.durable._orchestrator.run_orchestration`)
  and the run activity
  (:func:`~azure_functions_langgraph.durable._activity.execute_langgraph_run_impl`),
  and
* registers the three lifecycle HTTP routes (create / get / cancel) that adapt
  ``func.HttpRequest`` to the pure handlers in
  :mod:`azure_functions_langgraph.durable._lifecycle`.

The pure request→response adapters (:func:`create_run_http`,
:func:`get_run_http`, :func:`cancel_run_http`) are module-level and take an
already-constructed :class:`DurableClientLike`, so the whole HTTP surface is
unit-testable with a fake client and no Durable Task backend. Only the thin
decorator registration in :func:`register_durable_runtime` needs the real SDK.

Identifier model (issue #408): ``run_id == Durable instance_id == platform
run_id`` — one stable id, no second registry; Durable instance state is the
single source of truth for lifecycle status.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

import azure.functions as func

from azure_functions_langgraph.durable._activity import (
    GraphRegistrationLike,
    execute_langgraph_run_impl,
)
from azure_functions_langgraph.durable._lifecycle import (
    DurableClientLike,
    cancel_run_impl,
    create_run_impl,
    get_run_impl,
)
from azure_functions_langgraph.durable._orchestrator import run_orchestration
from azure_functions_langgraph.durable._state import DurableRunPayload, DurableRunResult
from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.observability import NOOP_OBSERVER, RunObserver

__all__ = [
    "ORCHESTRATOR_NAME",
    "DurableRuntimeDeps",
    "create_durable_function_app",
    "register_durable_runtime",
    "create_run_http",
    "get_run_http",
    "cancel_run_http",
]

# Friendly install hint surfaced when the optional ``durable`` extra is missing.
_EXTRA_HINT = (
    "Durable async runs require the optional 'durable' extra. "
    "Install it with: pip install azure-functions-langgraph[durable]"
)

# Stable Durable orchestrator function name. Referenced by ``create_run_impl``
# when starting a new orchestration; must match the registered orchestrator.
ORCHESTRATOR_NAME = "langgraph_durable_run_orchestrator"

# Lifecycle HTTP route templates (relative to the host ``routePrefix``).
_ROUTE_CREATE_RUN = "graphs/{name}/runs"
_ROUTE_GET_RUN = "runs/{run_id}"
_ROUTE_CANCEL_RUN = "runs/{run_id}/cancel"


def _import_durable() -> Any:
    """Import ``azure.durable_functions`` or raise a friendly ImportError."""

    try:
        import azure.durable_functions as df
    except ImportError as exc:  # pragma: no cover - exercised via the extra guard
        raise ImportError(_EXTRA_HINT) from exc
    return df


@dataclass(frozen=True)
class DurableRuntimeDeps:
    """Everything the durable runtime needs from the owning ``LangGraphApp``.

    Declared with structural types so the runtime never imports the app's
    concrete registration record (avoiding an import cycle).
    """

    registry: Mapping[str, GraphRegistrationLike]
    thread_lock: ThreadLock
    observer: RunObserver = NOOP_OBSERVER
    max_request_body_bytes: int = 1024 * 1024


def create_durable_function_app(auth_level: func.AuthLevel) -> func.FunctionApp:
    """Construct a ``df.DFApp`` typed as a ``func.FunctionApp`` for the caller.

    ``DFApp`` is not literally a ``func.FunctionApp`` subclass, but it inherits
    the same trigger/binding/registration surface (``.route``,
    ``.function_name``, ``.service_bus_*_trigger``) *plus* the durable bindings,
    and the worker indexes it identically. The return is annotated as
    ``func.FunctionApp`` so the rest of the app treats it uniformly.
    """

    df = _import_durable()
    app = df.DFApp(http_auth_level=auth_level)
    return app  # type: ignore[no-any-return]


def _json_response(status_code: int, body: Mapping[str, Any]) -> func.HttpResponse:
    """Build a JSON ``func.HttpResponse`` for a lifecycle result."""

    return func.HttpResponse(
        body=json.dumps(body),
        mimetype="application/json",
        status_code=status_code,
    )


def _parse_body(
    req: func.HttpRequest, *, max_bytes: int
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Parse a JSON object request body, returning ``(body, error)``.

    An empty body is treated as ``{}``. A body larger than ``max_bytes``, or one
    that is not a JSON object, yields an error string (and a ``None`` body).
    """

    raw = req.get_body() or b""
    if len(raw) > max_bytes:
        return None, f"request body exceeds {max_bytes} bytes"
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None, "request body is not valid JSON"
    if not isinstance(parsed, Mapping):
        return None, "request body must be a JSON object"
    return parsed, None


async def create_run_http(
    client: DurableClientLike,
    req: func.HttpRequest,
    *,
    registry: Mapping[str, GraphRegistrationLike],
    orchestrator_name: str = ORCHESTRATOR_NAME,
    max_request_body_bytes: int = 1024 * 1024,
) -> func.HttpResponse:
    """Adapt ``POST graphs/{name}/runs`` to :func:`create_run_impl`."""

    graph_name = req.route_params.get("name", "")
    if graph_name not in registry:
        return _json_response(404, {"error": f"no graph registered under name {graph_name!r}"})

    body, error = _parse_body(req, max_bytes=max_request_body_bytes)
    if error is not None:
        return _json_response(400, {"error": error})
    assert body is not None  # for type-checkers; error is None here

    status_code, result = await create_run_impl(
        client, orchestrator_name, body, graph_name=graph_name
    )
    return _json_response(status_code, result)


async def get_run_http(client: DurableClientLike, req: func.HttpRequest) -> func.HttpResponse:
    """Adapt ``GET runs/{run_id}`` to :func:`get_run_impl`."""

    run_id = req.route_params.get("run_id", "")
    status_code, result = await get_run_impl(client, run_id)
    return _json_response(status_code, result)


async def cancel_run_http(client: DurableClientLike, req: func.HttpRequest) -> func.HttpResponse:
    """Adapt ``POST runs/{run_id}/cancel`` to :func:`cancel_run_impl`."""

    run_id = req.route_params.get("run_id", "")
    status_code, result = await cancel_run_impl(client, run_id)
    return _json_response(status_code, result)


def register_durable_runtime(app: func.FunctionApp, deps: DurableRuntimeDeps) -> None:
    """Register the orchestrator, activity, and lifecycle routes on ``app``.

    ``app`` must be a ``df.DFApp`` (as returned by
    :func:`create_durable_function_app`). The graph registry, thread lock, and
    observer are captured in closures so the activity resolves and runs graphs
    exactly like the Service Bus trigger, while the orchestrator stays a pure
    deterministic generator.
    """

    registry = deps.registry
    thread_lock = deps.thread_lock
    observer = deps.observer
    max_body = deps.max_request_body_bytes

    # --- Deterministic orchestrator -------------------------------------
    @app.orchestration_trigger(context_name="context")  # type: ignore[untyped-decorator]
    def langgraph_durable_run_orchestrator(context: Any) -> Any:
        return run_orchestration(context)

    # --- Run activity (all user/graph code lives here) ------------------
    @app.activity_trigger(input_name="payload")  # type: ignore[untyped-decorator]
    async def execute_langgraph_run(payload: Any) -> dict[str, Any]:
        parsed = DurableRunPayload.from_dict(payload)
        result: DurableRunResult = await execute_langgraph_run_impl(
            parsed,
            registry=registry,
            thread_lock=thread_lock,
            observer=observer,
        )
        return result.to_dict()

    # --- Lifecycle HTTP routes ------------------------------------------
    @app.function_name(name="aflg_durable_create_run")
    @app.route(route=_ROUTE_CREATE_RUN, methods=["POST"])
    @app.durable_client_input(client_name="client")  # type: ignore[untyped-decorator]
    async def create_run(req: func.HttpRequest, client: DurableClientLike) -> func.HttpResponse:
        return await create_run_http(
            client,
            req,
            registry=registry,
            orchestrator_name=ORCHESTRATOR_NAME,
            max_request_body_bytes=max_body,
        )

    @app.function_name(name="aflg_durable_get_run")
    @app.route(route=_ROUTE_GET_RUN, methods=["GET"])
    @app.durable_client_input(client_name="client")  # type: ignore[untyped-decorator]
    async def get_run(req: func.HttpRequest, client: DurableClientLike) -> func.HttpResponse:
        return await get_run_http(client, req)

    @app.function_name(name="aflg_durable_cancel_run")
    @app.route(route=_ROUTE_CANCEL_RUN, methods=["POST"])
    @app.durable_client_input(client_name="client")  # type: ignore[untyped-decorator]
    async def cancel_run(req: func.HttpRequest, client: DurableClientLike) -> func.HttpResponse:
        return await cancel_run_http(client, req)
