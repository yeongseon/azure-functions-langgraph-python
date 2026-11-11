"""Regression tests for #854: per-graph auth must not be bypassed by optional routes."""

from __future__ import annotations

from typing import Any

import azure.functions as func
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import AuthorizationAction, AuthorizationContext

ANON = func.AuthLevel.ANONYMOUS
FUNCTION = func.AuthLevel.FUNCTION
ADMIN = func.AuthLevel.ADMIN


class _Graph:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        return {"ok": True}


def _app(app_level: func.AuthLevel, graph_level: func.AuthLevel | None, **kwargs: Any) -> Any:
    app = LangGraphApp(auth_level=app_level, **kwargs)
    app.register(_Graph(), name="agent", auth_level=graph_level)
    return app


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize(
    ("app_level", "graph_level"),
    [(ANON, FUNCTION), (ANON, ADMIN), (FUNCTION, ADMIN)],
)
def test_platform_routes_reject_stricter_graph_auth(
    app_level: func.AuthLevel, graph_level: func.AuthLevel
) -> None:
    app = _app(app_level, graph_level, platform_compat=True)
    with pytest.raises(ValueError, match=r"'agent'.*platform_compat"):
        app.function_app  # noqa: B018


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_durable_routes_reject_stricter_graph_auth() -> None:
    app = _app(ANON, FUNCTION, async_runs="durable")
    with pytest.raises(ValueError, match=r"'agent'.*async_runs"):
        app.function_app  # noqa: B018


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.parametrize(
    ("app_level", "graph_level"),
    [
        (ANON, ANON),
        (ANON, None),
        (FUNCTION, FUNCTION),
        (FUNCTION, ANON),
        (ADMIN, FUNCTION),
        (ADMIN, ADMIN),
    ],
)
def test_platform_routes_allow_non_exposing_combinations(
    app_level: func.AuthLevel, graph_level: func.AuthLevel | None
) -> None:
    assert _app(app_level, graph_level, platform_compat=True).function_app is not None


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_native_only_mixed_auth_still_builds() -> None:
    assert _app(ANON, FUNCTION).function_app is not None


def _handlers(app: LangGraphApp) -> dict[str, Any]:
    return {
        str(f.get_function_name()): f.get_user_function() for f in app.function_app.get_functions()
    }


def test_platform_run_authorizer_receives_resolved_graph_name_and_denies_before_invoke() -> None:
    seen: list[AuthorizationContext] = []

    class _Policy:
        def __call__(self, context: AuthorizationContext) -> bool:
            seen.append(context)
            return context.graph_name != "secret"

    graph = _Graph()
    app = LangGraphApp(platform_compat=True, authorize=_Policy())
    app.register(graph, name="secret")

    handlers = _handlers(app)
    for fn, route in (
        ("aflg_platform_runs_wait_threadless", "runs/wait"),
        ("aflg_platform_runs_stream_threadless", "runs/stream"),
    ):
        resp = handlers[fn](
            func.HttpRequest(
                method="POST",
                url=f"http://localhost/api/{route}",
                body=b'{"assistant_id": "secret", "input": {}}',
            )
        )
        assert resp.status_code == 403
    assert graph.calls == 0
    runs = [c for c in seen if c.action == AuthorizationAction.RUN]
    assert any(c.graph_name == "secret" for c in runs)
