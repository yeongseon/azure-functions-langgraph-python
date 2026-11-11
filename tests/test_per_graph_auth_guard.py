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


# ---------------------------------------------------------------------------
# Late registration after export (Copilot review on #868)
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_late_unsafe_registration_after_platform_export_is_rejected_without_mutation() -> None:
    app = LangGraphApp(auth_level=ANON, platform_compat=True)
    app.register(_Graph(), name="open")
    exported = app.function_app

    late = _Graph()
    with pytest.raises(ValueError, match=r"'secret'.*after.*routes"):
        app.register(late, name="secret", auth_level=FUNCTION)

    assert "secret" not in app._registrations
    assert app.function_app is exported
    handler = _handlers(app)["aflg_platform_runs_wait_threadless"]
    resp = handler(
        func.HttpRequest(
            method="POST",
            url="http://localhost/api/runs/wait",
            body=b'{"assistant_id": "secret", "input": {}}',
        )
    )
    assert resp.status_code == 404
    assert late.calls == 0


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_late_unsafe_registration_after_durable_export_is_rejected() -> None:
    app = LangGraphApp(auth_level=ANON, async_runs="durable")
    app.register(_Graph(), name="open")
    app.function_app  # noqa: B018
    with pytest.raises(ValueError, match=r"'secret'.*after.*routes"):
        app.register(_Graph(), name="secret", auth_level=ADMIN)
    assert "secret" not in app._registrations


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_late_safe_registration_after_export_still_allowed() -> None:
    app = LangGraphApp(auth_level=FUNCTION, platform_compat=True)
    app.register(_Graph(), name="open")
    app.function_app  # noqa: B018
    app.register(_Graph(), name="looser", auth_level=ANON)
    app.register(_Graph(), name="same", auth_level=FUNCTION)
    assert {"looser", "same"} <= set(app._registrations)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_late_stricter_registration_allowed_on_native_only_export() -> None:
    app = LangGraphApp(auth_level=ANON)
    app.register(_Graph(), name="open")
    app.function_app  # noqa: B018
    app.register(_Graph(), name="secret", auth_level=FUNCTION)
    assert app.function_app is not None


# ---------------------------------------------------------------------------
# Graph-aware authorization precedes thread lookup (Copilot review on #868)
# ---------------------------------------------------------------------------


class _DenySecretPolicy:
    def __init__(self) -> None:
        self.seen: list[AuthorizationContext] = []

    def __call__(self, context: AuthorizationContext) -> bool:
        self.seen.append(context)
        return context.graph_name != "secret"


@pytest.mark.parametrize("route", ["wait", "stream"])
@pytest.mark.parametrize("thread_exists", [True, False])
def test_threaded_run_denial_is_uniform_and_precedes_thread_lookup(
    route: str, thread_exists: bool
) -> None:
    policy = _DenySecretPolicy()
    graph = _Graph()
    app = LangGraphApp(platform_compat=True, authorize=policy)
    app.register(graph, name="secret")
    store = app._thread_store
    thread_id = store.create().thread_id if thread_exists else "missing-thread"
    before = store.get(thread_id)

    resp = _handlers(app)[f"aflg_platform_runs_{route}"](
        func.HttpRequest(
            method="POST",
            url=f"http://localhost/api/threads/{thread_id}/runs/{route}",
            route_params={"thread_id": thread_id},
            body=b'{"assistant_id": "secret", "input": {}}',
        )
    )

    assert resp.status_code == 403
    assert graph.calls == 0
    after = store.get(thread_id)
    assert after == before
    graph_aware = [c for c in policy.seen if c.graph_name == "secret"]
    assert graph_aware
    assert all(c.thread_id == thread_id for c in graph_aware)
    assert all(c.action == AuthorizationAction.RUN for c in graph_aware)


@pytest.mark.parametrize("route", ["wait", "stream"])
def test_threaded_run_missing_thread_still_404_when_allowed(route: str) -> None:
    policy = _DenySecretPolicy()
    app = LangGraphApp(platform_compat=True, authorize=policy)
    app.register(_Graph(), name="open")
    resp = _handlers(app)[f"aflg_platform_runs_{route}"](
        func.HttpRequest(
            method="POST",
            url=f"http://localhost/api/threads/nope/runs/{route}",
            route_params={"thread_id": "nope"},
            body=b'{"assistant_id": "open", "input": {}}',
        )
    )
    assert resp.status_code == 404
