"""Binding-discovery snapshot captured before extracting route stages."""

import inspect

from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _build_platform_app


def test_registered_function_inventory_is_unchanged() -> None:
    # Given the complete function app, including native and Platform routes.
    app = _build_platform_app(graphs={"agent": FakeCompiledGraph()})
    expected = [
        ("aflg_health", "health", "health", "GET", "anonymous"),
        ("aflg_health_details", "health_details", "health/details", "GET", "function"),
        ("aflg_agent_invoke", "sync_handler", "graphs/agent/invoke", "POST", "function"),
        ("aflg_agent_stream", "sync_handler", "graphs/agent/stream", "POST", "function"),
        (
            "aflg_platform_assistants_search",
            "assistants_search",
            "assistants/search",
            "POST",
            "function",
        ),
        (
            "aflg_platform_assistants_count",
            "assistants_count",
            "assistants/count",
            "POST",
            "function",
        ),
        (
            "aflg_platform_assistants_get",
            "assistants_get",
            "assistants/{assistant_id}",
            "GET",
            "function",
        ),
        ("aflg_platform_threads_create", "threads_create", "threads", "POST", "function"),
        ("aflg_platform_threads_get", "threads_get", "threads/{thread_id}", "GET", "function"),
        (
            "aflg_platform_threads_update",
            "threads_update",
            "threads/{thread_id}",
            "PATCH",
            "function",
        ),
        (
            "aflg_platform_threads_delete",
            "threads_delete",
            "threads/{thread_id}",
            "DELETE",
            "function",
        ),
        ("aflg_platform_threads_search", "threads_search", "threads/search", "POST", "function"),
        ("aflg_platform_threads_count", "threads_count", "threads/count", "POST", "function"),
        (
            "aflg_platform_threads_state_get",
            "threads_state_get",
            "threads/{thread_id}/state",
            "GET",
            "function",
        ),
        (
            "aflg_platform_threads_state_update",
            "threads_state_update",
            "threads/{thread_id}/state",
            "POST",
            "function",
        ),
        (
            "aflg_platform_threads_history",
            "threads_history",
            "threads/{thread_id}/history",
            "POST",
            "function",
        ),
        (
            "aflg_platform_runs_wait",
            "runs_wait",
            "threads/{thread_id}/runs/wait",
            "POST",
            "function",
        ),
        (
            "aflg_platform_runs_stream",
            "runs_stream",
            "threads/{thread_id}/runs/stream",
            "POST",
            "function",
        ),
        (
            "aflg_platform_runs_wait_threadless",
            "runs_wait_threadless",
            "runs/wait",
            "POST",
            "function",
        ),
        (
            "aflg_platform_runs_stream_threadless",
            "runs_stream_threadless",
            "runs/stream",
            "POST",
            "function",
        ),
    ]
    # When Azure binding discovery enumerates the registered functions.
    functions = app.function_app.get_functions()
    actual = []
    for function in functions:
        handler = function.get_user_function()
        bindings = function.get_bindings_dict()["bindings"]
        trigger, output = bindings
        actual.append(
            (
                function.get_function_name(),
                handler.__name__,
                trigger["route"],
                ",".join(method.value for method in trigger["methods"]),
                trigger["authLevel"].value,
            )
        )
        assert str(inspect.signature(handler)) == "(req: 'func.HttpRequest') -> 'func.HttpResponse'"
        assert (trigger["type"], trigger["name"], trigger["direction"].name) == (
            "httpTrigger",
            "req",
            "IN",
        )
        assert (output["type"], output["name"], output["direction"].name) == (
            "http",
            "$return",
            "OUT",
        )
    # Then names, Python entrypoints, signatures and every binding remain identical.
    assert actual == expected
