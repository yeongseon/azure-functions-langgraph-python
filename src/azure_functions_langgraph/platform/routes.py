"""Platform API–compatible route registration.

Registers Azure Functions HTTP routes that mirror the LangGraph Platform
REST API.  When enabled via
``LangGraphApp(platform_compat=True)``, the official ``langgraph-sdk``
Python client can communicate with Azure Functions–hosted graphs.

Routes registered (under ``/api/`` prefix, managed by Azure Functions):

* ``POST /api/assistants/search``
* ``POST /api/assistants/count``
* ``GET  /api/assistants/{assistant_id}``
* ``POST /api/threads``
* ``GET  /api/threads/{thread_id}``
* ``PATCH /api/threads/{thread_id}``
* ``DELETE /api/threads/{thread_id}``
* ``POST /api/threads/search``
* ``POST /api/threads/count``
* ``GET  /api/threads/{thread_id}/state``
* ``POST /api/threads/{thread_id}/state``
* ``POST /api/threads/{thread_id}/history``
* ``POST /api/threads/{thread_id}/runs/wait``
* ``POST /api/threads/{thread_id}/runs/stream``
* ``POST /api/runs/wait``  *(threadless)*
* ``POST /api/runs/stream``  *(threadless)*

.. versionadded:: 0.3.0
"""

from __future__ import annotations

import azure.functions as func

from azure_functions_langgraph.platform._assistants import register_assistant_routes
from azure_functions_langgraph.platform._common import (
    PlatformRouteDeps,
    _get_threadless_graph,
    _platform_error,
    _preflight_run_create,
    _registration_to_assistant,
    _snapshot_to_thread_state,
)
from azure_functions_langgraph.platform._runs import register_run_routes
from azure_functions_langgraph.platform._threads import register_thread_routes


def register_platform_routes(
    app: func.FunctionApp,
    deps: PlatformRouteDeps,
) -> None:
    """Register all LangGraph Platform–compatible routes on *app*.

    Parameters
    ----------
    app:
        The Azure ``FunctionApp`` to register routes on.
    deps:
        Narrow dependency bag containing registrations, thread store,
        auth level, and max stream bytes.
    """
    register_assistant_routes(app, deps)
    register_thread_routes(app, deps)
    register_run_routes(app, deps)


__all__ = [
    "PlatformRouteDeps",
    "register_platform_routes",
    "_platform_error",
    "_get_threadless_graph",
    "_preflight_run_create",
    "_registration_to_assistant",
    "_snapshot_to_thread_state",
]
