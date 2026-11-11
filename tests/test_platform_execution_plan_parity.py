"""Platform request-planning parity through the real Platform handlers (#861).

Companion to ``tests/test_execution_plan_parity.py`` (Native/Streaming). These
tests pin what the four Platform run routes (``threads/{id}/runs/wait|stream``
and ``runs/wait|stream``) do for the planning stages the other surfaces also
perform: config/thread admission, optional-capability negotiation (Platform's
analog of the native ``version`` rule is ``context``), optional-model
validation at ``register()``, authorization ordering, observer context,
serialization of the result and error shapes. Where Platform deliberately
differs from Native (wire contract, status codes, unknown-field policy) the test
says so, so a future shared helper cannot silently erase the distinction.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

import azure.functions as func
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph._execution_plan import extract_thread_id
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    ThreadAuthorizer,
)
from azure_functions_langgraph.observability import RunContext
from tests.auth_boundary_support import handler

THREADED = ("aflg_platform_runs_wait", "aflg_platform_runs_stream")
THREADLESS = ("aflg_platform_runs_wait_threadless", "aflg_platform_runs_stream_threadless")
ALL_ROUTES = THREADED + THREADLESS


class _Graph:
    """Structural graph; ``accepts_context`` toggles the ``context`` parameter."""

    def __init__(self, *, checkpointed: bool = False, accepts_context: bool = False) -> None:
        self.checkpointer = InMemorySaver() if checkpointed else None
        self.calls: list[dict[str, Any]] = []
        self.copies = 0
        if accepts_context:
            self.invoke = self._invoke_ctx  # type: ignore[method-assign]
            self.stream = self._stream_ctx  # type: ignore[method-assign]

    def copy(self, *, update: dict[str, Any]) -> _Graph:
        clone = _Graph()
        clone.calls = self.calls
        clone.copies = self.copies + 1
        for key, value in update.items():
            setattr(clone, key, value)
        return clone

    def invoke(self, input: dict[str, Any], config: Any = None) -> Any:
        self.calls.append({"config": config, "context": None})
        return {"ok": True}

    def _invoke_ctx(self, input: dict[str, Any], config: Any = None, context: Any = None) -> Any:
        self.calls.append({"config": config, "context": context})
        return {"ok": True}

    def stream(self, input: dict[str, Any], config: Any = None, stream_mode: str = "values") -> Any:
        self.calls.append({"config": config, "context": None})
        yield {"chunk": 1}

    def _stream_ctx(
        self,
        input: dict[str, Any],
        config: Any = None,
        stream_mode: str = "values",
        context: Any = None,
    ) -> Any:
        self.calls.append({"config": config, "context": context})
        yield {"chunk": 1}


class _Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, RunContext]] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append(("started", ctx))

    def on_run_completed(self, ctx: RunContext) -> None:
        self.events.append(("completed", ctx))

    def on_run_failed(self, ctx: RunContext, exc: BaseException) -> None:
        self.events.append(("failed", ctx))

    def on_run_rejected(self, ctx: RunContext, reason: Any) -> None:
        self.events.append(("rejected", ctx))


def _app(
    graph: Any = None,
    *,
    authorize: ThreadAuthorizer | None = None,
    observer: Any = None,
    name: str = "agent",
) -> tuple[LangGraphApp, Any]:
    graph = graph if graph is not None else _Graph()
    app = LangGraphApp(platform_compat=True, authorize=authorize, observer=observer)
    app.register(graph, name=name)
    return app, graph


def _call(
    app: LangGraphApp,
    route: str,
    payload: Any,
    *,
    thread_id: str | None = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> func.HttpResponse:
    threaded = route in THREADED
    if threaded and thread_id is None:
        thread_id = app._thread_store.create().thread_id
    kind = "wait" if route.endswith(("wait", "wait_threadless")) else "stream"
    url = (
        f"http://localhost/api/threads/{thread_id}/runs/{kind}"
        if threaded
        else (f"http://localhost/api/runs/{kind}")
    )
    body = raw if raw is not None else json.dumps(payload).encode()
    return handler(app, route)(
        func.HttpRequest(
            method="POST",
            url=url,
            route_params={"thread_id": thread_id} if threaded and thread_id else {},
            headers=headers or {},
            body=body,
        )
    )


def _detail(resp: func.HttpResponse) -> Any:
    return json.loads(resp.get_body())["detail"]


def _run(payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"assistant_id": "agent", "input": {}, **(payload or {}), **extra}


# --- config / thread admission -------------------------------------------------


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_non_object_configurable_is_400_with_platform_wording(route: str) -> None:
    app, graph = _app()
    resp = _call(app, route, _run(config={"configurable": "nope"}))
    # Native says "config.configurable must be an object"; Platform's wire text
    # allows null ("object or null"). Both are 400 and neither executes.
    assert resp.status_code == 400
    assert _detail(resp) == "config.configurable must be a JSON object or null"
    assert graph.calls == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_null_configurable_is_admitted(route: str) -> None:
    app, graph = _app()
    resp = _call(app, route, _run(config={"configurable": None}))
    assert resp.status_code == 200
    assert len(graph.calls) == 1
    assert graph.calls[0]["config"]["configurable"] is not None


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_config_structure_limits_are_400(route: str) -> None:
    app, graph = _app()
    deep: dict[str, Any] = {}
    cur = deep
    for _ in range(40):
        cur["k"] = {}
        cur = cur["k"]
    resp = _call(app, route, _run(config=deep))
    assert resp.status_code == 400
    assert graph.calls == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_unapproved_configurable_keys_are_not_rejected_on_platform(route: str) -> None:
    # Platform has no allow-list (the native `allowed_configurable_keys` policy
    # is a native-only rule); arbitrary configurable keys reach the graph.
    app, graph = _app()
    resp = _call(app, route, _run(config={"configurable": {"custom_key": 1}}))
    assert resp.status_code == 200
    assert graph.calls[0]["config"]["configurable"]["custom_key"] == 1


@pytest.mark.parametrize("route", THREADED)
@pytest.mark.parametrize("bad", ["x" * 1000, "bad\x01id", " " * 0 + "é"])
def test_invalid_path_thread_id_matches_shared_rule(route: str, bad: str) -> None:
    app, graph = _app()
    resp = _call(app, route, _run(), thread_id=bad)
    expected = extract_thread_id({"configurable": {"thread_id": bad}})[1]
    assert expected is not None
    assert resp.status_code == 400
    assert _detail(resp) == expected
    assert graph.calls == []


@pytest.mark.parametrize("route", THREADED)
def test_path_thread_id_pins_config_over_client_value(route: str) -> None:
    app, graph = _app(_Graph(checkpointed=True))
    tid = app._thread_store.create().thread_id
    resp = _call(
        app, route, _run(config={"configurable": {"thread_id": "other", "k": 1}}), thread_id=tid
    )
    assert resp.status_code == 200
    configurable = graph.calls[0]["config"]["configurable"]
    assert configurable["thread_id"] == tid
    assert configurable["k"] == 1


@pytest.mark.parametrize("route", THREADED)
def test_missing_thread_is_404(route: str) -> None:
    app, graph = _app()
    resp = _call(app, route, _run(), thread_id="missing")
    assert resp.status_code == 404
    assert _detail(resp) == "Thread 'missing' not found"
    assert graph.calls == []


@pytest.mark.parametrize("route", THREADLESS)
@pytest.mark.parametrize("tid", ["abc", 5, ""])
def test_threadless_rejects_client_thread_id_with_422(route: str, tid: Any) -> None:
    # Native allows a thread id on an unchecked graph; Platform threadless runs
    # are ephemeral by contract, so any non-null thread_id is a 422.
    app, graph = _app()
    resp = _call(app, route, _run(config={"configurable": {"thread_id": tid}}))
    assert resp.status_code == 422
    assert _detail(resp) == "thread_id is not allowed on threadless runs"
    assert graph.calls == []


@pytest.mark.parametrize("route", THREADLESS)
def test_threadless_null_thread_id_is_admitted(route: str) -> None:
    app, graph = _app()
    resp = _call(app, route, _run(config={"configurable": {"thread_id": None}}))
    assert resp.status_code == 200
    assert graph.calls[0]["config"]["configurable"]["thread_id"] is None


@pytest.mark.parametrize("route", THREADLESS)
def test_threadless_checkpointed_graph_runs_on_checkpointer_free_clone(route: str) -> None:
    app, graph = _app(_Graph(checkpointed=True))
    resp = _call(app, route, _run())
    assert resp.status_code == 200
    assert graph.copies == 0 and len(graph.calls) == 1  # clone shares the call log
    assert graph.checkpointer is not None  # source graph untouched


@pytest.mark.parametrize("route", THREADLESS)
def test_threadless_checkpointed_uncloneable_graph_is_501(route: str) -> None:
    class NoCopy:
        checkpointer = InMemorySaver()

        def invoke(self, input: Any, config: Any = None) -> Any:
            raise AssertionError("must not execute")

        def stream(
            self, input: Any, config: Any = None, stream_mode: str = "values"
        ) -> Iterator[Any]:
            raise AssertionError("must not execute")

    app, _ = _app(NoCopy())
    resp = _call(app, route, _run())
    assert resp.status_code == 501


# --- capability negotiation (Platform's analog of native `version`) ------------


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_context_forwarded_when_supported(route: str) -> None:
    app, graph = _app(_Graph(accepts_context=True))
    resp = _call(app, route, _run(context={"user": "u1"}))
    assert resp.status_code == 200
    assert graph.calls[0]["context"] == {"user": "u1"}


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_context_rejected_422_when_unsupported(route: str) -> None:
    app, graph = _app(_Graph(accepts_context=False))
    resp = _call(app, route, _run(context={"user": "u1"}))
    assert resp.status_code == 422
    assert _detail(resp) == "Graph 'agent' does not accept a 'context' argument"
    assert graph.calls == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_no_context_runs_on_plain_graph(route: str) -> None:
    app, graph = _app(_Graph(accepts_context=False))
    assert _call(app, route, _run()).status_code == 200
    assert len(graph.calls) == 1


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_kwargs_catch_all_accepts_context(route: str) -> None:
    class Kw(_Graph):
        def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
            self.calls.append({"config": config, "context": kwargs.get("context")})
            return {"ok": True}

        def stream(  # type: ignore[override]
            self, input: Any, config: Any = None, **kwargs: Any
        ) -> Any:
            self.calls.append({"config": config, "context": kwargs.get("context")})
            yield {"chunk": 1}

    app, graph = _app(Kw())
    assert _call(app, route, _run(context={"a": 1})).status_code == 200
    assert graph.calls[0]["context"] == {"a": 1}


def test_there_is_no_version_negotiation_on_platform() -> None:
    # `version` is a native request field; Platform routes do not forward it.
    app, graph = _app(_Graph())
    resp = _call(app, "aflg_platform_runs_wait_threadless", _run(version="v2"))
    assert resp.status_code == 200
    assert graph.calls[0]["context"] is None


# --- optional-model validation (register-time, shared by every surface) --------


class _NotModel:
    pass


class _Model(BaseModel):
    x: int = 0


@pytest.mark.parametrize("label", ["request_model", "response_model"])
def test_register_rejects_non_model_with_platform_compat(label: str) -> None:
    app = LangGraphApp(platform_compat=True)
    bad: dict[str, Any] = {label: _NotModel}
    good: dict[str, Any] = {label: _Model}
    with pytest.raises(TypeError, match=label):
        app.register(_Graph(), name="agent", **bad)
    app.register(_Graph(), name="ok", **good)


# --- authorization ordering ----------------------------------------------------


class _Policy:
    def __init__(self, deny_graph: str | None = None, deny_all: bool = False) -> None:
        self.seen: list[AuthorizationContext] = []
        self.deny_graph = deny_graph
        self.deny_all = deny_all

    def __call__(self, context: AuthorizationContext) -> bool:
        self.seen.append(context)
        if self.deny_all:
            return False
        return self.deny_graph is None or context.graph_name != self.deny_graph


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_route_level_denial_precedes_body_parsing(route: str) -> None:
    policy = _Policy(deny_all=True)
    app, graph = _app(authorize=policy)
    resp = _call(app, route, None, raw=b"{not json", thread_id="t" if route in THREADED else None)
    assert resp.status_code == 403
    assert policy.seen[0].graph_name is None
    assert policy.seen[0].action == AuthorizationAction.RUN
    assert graph.calls == []


@pytest.mark.parametrize("route", THREADED)
def test_thread_id_validity_precedes_body_parsing(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, None, raw=b"{not json", thread_id="x" * 1000)
    assert resp.status_code == 400
    assert "maximum length" in _detail(resp)


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_graph_aware_denial_precedes_structure_and_capability_checks(route: str) -> None:
    # #868: graph-aware authorization runs before config/context/thread work, so
    # a denied graph never reveals structure, capability or thread existence.
    policy = _Policy(deny_graph="agent")
    app, graph = _app(authorize=policy)
    resp = _call(
        app,
        route,
        _run(config={"configurable": "bad"}, context={"a": 1}),
        thread_id="missing" if route in THREADED else None,
    )
    assert resp.status_code == 403
    assert _detail(resp) == "Access denied"
    assert any(c.graph_name == "agent" for c in policy.seen)
    assert graph.calls == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_unknown_assistant_is_404_before_graph_authorization(route: str) -> None:
    policy = _Policy()
    app, _ = _app(authorize=policy)
    resp = _call(
        app, route, _run(assistant_id="ghost"), thread_id="missing" if route in THREADED else None
    )
    assert resp.status_code == 404
    assert _detail(resp) == "Assistant 'ghost' not found"
    assert all(c.graph_name is None for c in policy.seen)


@pytest.mark.parametrize("route", THREADED)
def test_threaded_thread_lookup_precedes_structure_validation(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, _run(config={"configurable": "bad"}), thread_id="missing")
    assert resp.status_code == 404


@pytest.mark.parametrize("route", THREADLESS)
def test_threadless_structure_validation_precedes_thread_id_rejection(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, _run(config={"configurable": {"thread_id": "x"}, "deep": _deep(40)}))
    assert resp.status_code == 400


def _deep(levels: int) -> dict[str, Any]:
    root: dict[str, Any] = {}
    cur = root
    for _ in range(levels):
        cur["k"] = {}
        cur = cur["k"]
    return root


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_unsupported_feature_501_precedes_graph_authorization(route: str) -> None:
    policy = _Policy(deny_graph="agent")
    app, _ = _app(authorize=policy)
    resp = _call(
        app, route, _run(interrupt_before=["x"]), thread_id="t" if route in THREADED else None
    )
    assert resp.status_code == 501


# --- request preamble: shapes and status-code precedence -----------------------


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_invalid_json_is_400_detail_shape(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, None, raw=b"{nope")
    assert resp.status_code == 400
    assert resp.mimetype == "application/json"
    assert json.loads(resp.get_body()) == {"detail": "Invalid JSON body"}


@pytest.mark.parametrize("route", THREADLESS)
def test_threadless_requires_object_body(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, [1])
    assert resp.status_code == 400
    assert _detail(resp) == "Request body must be a JSON object"


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_model_validation_failure_is_422_with_list_detail(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, {"input": {}})  # assistant_id missing
    assert resp.status_code == 422
    assert isinstance(_detail(resp), list)


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_invalid_assistant_name_is_400(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, _run(assistant_id="bad name!"))
    assert resp.status_code == 400


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_oversized_body_is_413(route: str) -> None:
    app, _ = _app()
    app.max_request_body_bytes = 10
    resp = _call(app, route, _run())
    assert resp.status_code == 413


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_unknown_field_default_warns_and_runs(route: str) -> None:
    app, graph = _app()
    with pytest.warns(UserWarning, match="unknown field"):
        resp = _call(app, route, _run(zzz=1))
    assert resp.status_code == 200
    assert len(graph.calls) == 1


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_unknown_field_strict_mode_is_400(route: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZFUNC_LANGGRAPH_PLATFORM_STRICT", "1")
    app, graph = _app()
    resp = _call(app, route, _run(zzz=1))
    assert resp.status_code == 400
    assert "zzz" in _detail(resp)
    assert graph.calls == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_graph_failure_is_sanitized_with_request_id(route: str) -> None:
    class Boom(_Graph):
        def invoke(self, input: Any, config: Any = None) -> Any:
            raise RuntimeError("secret")

        def stream(self, input: Any, config: Any = None, stream_mode: str = "values") -> Any:
            raise RuntimeError("secret")
            yield  # pragma: no cover

    app, _ = _app(Boom())
    resp = _call(app, route, _run(), headers={"X-Request-ID": "rid-1"})
    assert "secret" not in resp.get_body().decode()
    if route in ("aflg_platform_runs_wait", "aflg_platform_runs_wait_threadless"):
        assert resp.status_code == 500
        assert json.loads(resp.get_body()) == {
            "detail": "Graph execution failed",
            "request_id": "rid-1",
        }
        assert resp.headers.get("X-Request-ID") == "rid-1"
    else:
        # The metadata event is already buffered, so a mid-stream failure is a
        # 200 SSE carrying a sanitized error event and the terminal end event.
        assert resp.status_code == 200
        body = resp.get_body().decode()
        assert "stream processing failed" in body and "rid-1" in body
        assert "event: end" in body


# --- observer context ----------------------------------------------------------


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_observer_context_carries_identity(route: str) -> None:
    obs = _Observer()
    app, _ = _app(_Graph(checkpointed=route in THREADED), observer=obs)
    resp = _call(app, route, _run(), thread_id=None, headers={"X-Request-ID": "rid-2"})
    assert resp.status_code == 200
    assert [kind for kind, _ in obs.events] == ["started", "completed"]
    ctx = obs.events[0][1]
    assert ctx.graph_name == "agent"
    assert ctx.run_id == "rid-2"
    assert ctx.endpoint == ("invoke" if "wait" in route else "stream")
    assert (ctx.thread_id is not None) == (route in THREADED)
    assert ctx.has_checkpointer == (route in THREADED)


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_rejected_before_execution_emits_no_observer_events(route: str) -> None:
    obs = _Observer()
    app, _ = _app(_Graph(accepts_context=False), observer=obs)
    resp = _call(app, route, _run(context={"a": 1}))
    assert resp.status_code == 422
    assert obs.events == []


# --- serialization of results --------------------------------------------------


@pytest.mark.parametrize("route", ("aflg_platform_runs_wait", "aflg_platform_runs_wait_threadless"))
def test_non_dict_result_is_wrapped(route: str) -> None:
    class Scalar(_Graph):
        def invoke(self, input: Any, config: Any = None) -> Any:
            return [1, 2]

    app, _ = _app(Scalar())
    resp = _call(app, route, _run())
    assert resp.status_code == 200
    assert json.loads(resp.get_body()) == {"result": [1, 2]}


@pytest.mark.parametrize(
    "route", ("aflg_platform_runs_stream", "aflg_platform_runs_stream_threadless")
)
def test_stream_routes_buffer_sse(route: str) -> None:
    app, _ = _app()
    resp = _call(app, route, _run())
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"
    body = resp.get_body().decode()
    assert body.startswith("event: metadata")
    assert body.rstrip().endswith("event: end\ndata: null") or "event: end" in body


def test_uninspectable_method_is_treated_as_not_accepting_context() -> None:
    from azure_functions_langgraph._execution_plan import method_accepts_kwarg
    from azure_functions_langgraph.platform._common import (
        _method_accepts_context,
        _resolve_context_kwarg,
    )

    class Opaque:
        @property
        def __signature__(self) -> Any:
            raise ValueError("no signature")

        def __call__(self, *args: Any) -> None: ...

    assert method_accepts_kwarg(Opaque(), "context") is False
    assert _method_accepts_context(Opaque()) is False
    result = _resolve_context_kwarg(Opaque(), {"a": 1}, "agent")
    assert isinstance(result, func.HttpResponse) and result.status_code == 422
    assert _resolve_context_kwarg(Opaque(), None, "agent") == {}


def test_kwarg_probe_is_shared_between_version_and_context() -> None:
    from azure_functions_langgraph._execution_plan import (
        method_accepts_kwarg,
        method_accepts_version,
    )

    def with_version(x: Any, version: str | None = None) -> None: ...
    def with_context(x: Any, context: Any = None) -> None: ...

    assert method_accepts_version(with_version) and not method_accepts_version(with_context)
    assert method_accepts_kwarg(with_context, "context")
    assert not method_accepts_kwarg(with_version, "context")
