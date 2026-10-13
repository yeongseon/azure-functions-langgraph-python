"""Smoke test that keeps the 5-minute tutorial's commands honest.

``docs/tutorial-5-min.md`` documents copy-pasteable ``curl`` commands against the
shipped ``examples/simple_agent`` app. This test drives the *exact* documented
invoke payload through the same native HTTP handler the deployed app uses and
asserts the documented response — so the tutorial cannot silently drift away from
the example's real behavior.

Issue: #370
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import azure.functions as func

from azure_functions_langgraph.app import LangGraphApp

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "examples" / "simple_agent"
GRAPH_NAME = "simple_agent"
ROOT_README = Path(__file__).resolve().parent.parent / "README.md"


def _load_compiled_graph() -> Any:
    """Load ``compiled_graph`` from the real ``examples/simple_agent`` app."""
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "_tutorial_simple_agent_graph", EXAMPLE_DIR / "graph.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod.compiled_graph


def _get_handler(fa: func.FunctionApp, fn_name: str) -> Any:
    fa.functions_bindings = {}
    for fn in fa.get_functions():
        if fn.get_function_name() == fn_name:
            return fn.get_user_function()
    raise AssertionError(f"Function {fn_name!r} not found")


def _post(url: str, body: dict[str, Any], **route_params: str) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url=url,
        body=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        route_params=route_params,
    )


def test_tutorial_invoke_matches_documented_response() -> None:
    """The invoke curl in docs/tutorial-5-min.md returns the documented JSON."""
    app = LangGraphApp()
    app.register(
        graph=_load_compiled_graph(),
        name=GRAPH_NAME,
        description="A simple two-node greeting agent",
    )
    handler = _get_handler(app.function_app, f"aflg_{GRAPH_NAME}_invoke")

    # Exactly the payload documented in the tutorial's "Invoke the agent" curl.
    req = _post(
        f"/api/graphs/{GRAPH_NAME}/invoke",
        {"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}},
        name=GRAPH_NAME,
    )
    resp = handler(req)

    assert resp.status_code == 200
    output = json.loads(resp.get_body())["output"]

    # The documented response block.
    assert output["greeting"] == "Hello, World!"
    assert output["messages"] == [
        {"role": "human", "content": "World"},
        {"role": "assistant", "content": "Hello, World! Goodbye!"},
    ]


def test_tutorial_health_probe_is_anonymous_status_ok() -> None:
    """The health curl in the tutorial returns exactly {"status": "ok"}."""
    app = LangGraphApp()
    app.register(graph=_load_compiled_graph(), name=GRAPH_NAME)
    handler = _get_handler(app.function_app, "aflg_health")

    resp = handler(func.HttpRequest(method="GET", url="/api/health", body=b""))

    assert resp.status_code == 200
    assert json.loads(resp.get_body()) == {"status": "ok"}


def test_readme_see_it_working_matches_registered_simple_agent() -> None:
    # Given: the shipped example registered through the real adapter
    app = LangGraphApp()
    app.register(
        graph=_load_compiled_graph(),
        name=GRAPH_NAME,
        description="A simple two-node greeting agent",
    )
    metadata = app.get_app_metadata()
    routes = (*metadata.app_routes, *metadata.graphs[GRAPH_NAME].routes)
    documented_routes = "\n".join(sorted(f"{route.method} {route.path}" for route in routes))
    handlers = {
        fn.get_function_name(): fn.get_user_function() for fn in app.function_app.get_functions()
    }

    # When: the documented health and invoke requests run through registered handlers
    health = handlers["aflg_health"](func.HttpRequest(method="GET", url="/api/health", body=b""))
    invoke = handlers[f"aflg_{GRAPH_NAME}_invoke"](
        _post(
            f"/api/graphs/{GRAPH_NAME}/invoke",
            {
                "input": {
                    "messages": [{"role": "human", "content": "World"}],
                    "greeting": "",
                }
            },
            name=GRAPH_NAME,
        )
    )
    readme = ROOT_README.read_text()
    transcript = readme.split("### See it working", 1)[1].split("### Recommended learning path", 1)[
        0
    ]

    # Then: every stable route and response value in the transcript is live output
    assert documented_routes in transcript
    assert health.get_body().decode() in transcript
    assert invoke.get_body().decode() in transcript
    assert "/api/health/details" in transcript
    assert "protected" in transcript
    assert "docs/tutorial-5-min.md" in transcript
