"""Native RUN admission must precede checkpoint I/O and lock acquisition (#687)."""

from __future__ import annotations

from functools import partial
import json
from typing import Literal, TypedDict

import anyio
import azure.functions as func
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
import pytest

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    ThreadAuthorizer,
    UnauthenticatedError,
)
from azure_functions_langgraph.locks import InProcessThreadLock
from azure_functions_langgraph.observability import NoOpRunObserver, RunContext, RunRejectedReason


class State(TypedDict):
    value: str


class AdmissionLock(InProcessThreadLock):
    """Count attempts while retaining the real in-process locking behavior."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        self.attempts += 1
        return super().acquire(graph_name, thread_id, timeout)


class Observer(NoOpRunObserver):
    def __init__(self) -> None:
        self.rejections: list[RunRejectedReason] = []
        self.started = 0

    def on_run_started(self, ctx: RunContext) -> None:
        self.started += 1

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self.rejections.append(reason)


class Scenario:
    """Real compiled graph and admission instrumentation for one request."""

    def __init__(self, authorize: ThreadAuthorizer | None, thread_id: str | None) -> None:
        self.calls: list[State] = []
        self.lock = AdmissionLock()
        self.observer = Observer()

        def node(state: State) -> State:
            self.calls.append(state)
            return {"value": "executed"}

        builder = StateGraph(State)
        builder.add_node("node", node)
        builder.add_edge(START, "node")
        builder.add_edge("node", END)
        graph = builder.compile(checkpointer=InMemorySaver() if thread_id else None)
        self.app = LangGraphApp(authorize=authorize, thread_lock=self.lock, observer=self.observer)
        self.app.register(graph=graph, name="agent")
        config = {"configurable": {"thread_id": thread_id}} if thread_id is not None else {}
        self.request = func.HttpRequest(
            method="POST",
            url="https://example.test/api/graphs/agent/invoke",
            body=json.dumps({"input": {"value": "secret-input"}, "config": config}).encode(),
            headers={"X-Request-ID": "auth-test"},
        )

    async def run(
        self, endpoint: Literal["invoke", "stream"], async_mode: bool
    ) -> func.HttpResponse:
        reg = self.app._registrations["agent"]
        if endpoint == "invoke":
            if async_mode:
                return await self.app._handle_invoke_async(self.request, reg)
            return await anyio.to_thread.run_sync(
                partial(self.app._handle_invoke, self.request, reg)
            )
        if async_mode:
            return await self.app._handle_stream_async(self.request, reg)
        return await anyio.to_thread.run_sync(partial(self.app._handle_stream, self.request, reg))


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("thread_id", ["other-users-thread", None])
@pytest.mark.parametrize("unauthenticated", [False, True])
async def test_denied_run_never_executes_or_locks(
    endpoint: Literal["invoke", "stream"],
    async_mode: bool,
    thread_id: str | None,
    unauthenticated: bool,
) -> None:
    """RUN denial must precede checkpoint work, independent of STATE_READ permission."""
    # Given a policy explicitly denying RUN, independently of STATE_READ.
    contexts: list[AuthorizationContext] = []

    def authorize(context: AuthorizationContext) -> bool:
        contexts.append(context)
        if unauthenticated:
            raise UnauthenticatedError("secret-principal")
        return context.action != AuthorizationAction.RUN

    scenario = Scenario(authorize, thread_id)
    # When a native handler admits the request.
    response = await scenario.run(endpoint, async_mode)
    # Then no graph or checkpoint work, lock attempt, or start event occurred.
    assert response.status_code == (401 if unauthenticated else 403)
    assert json.loads(response.get_body()) == {
        "error": "error",
        "detail": "Access denied",
        "request_id": "auth-test",
    }
    assert contexts == [
        AuthorizationContext(AuthorizationAction.RUN, "agent", thread_id, scenario.request)
    ]
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert scenario.observer.started == 0
    assert scenario.observer.rejections == ["authorization_denied"]


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("decision", ["allow", "deny", "unauthenticated", "error"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("async_hook", [False, True])
async def test_async_authorizer_is_awaited_and_errors_are_safe(
    endpoint: Literal["invoke", "stream"],
    decision: str,
    async_mode: bool,
    async_hook: bool,
) -> None:
    # Given an async policy that must execute on the active event loop.
    contexts: list[AuthorizationContext] = []

    def authorize(context: AuthorizationContext) -> bool:
        contexts.append(context)
        if decision == "error":
            raise RuntimeError("secret-hook-error")
        if decision == "unauthenticated":
            raise UnauthenticatedError("secret-principal")
        return decision == "allow"

    async def async_authorize(context: AuthorizationContext) -> bool:
        return authorize(context)

    scenario = Scenario(async_authorize if async_hook else authorize, "thread-one")
    # When a native async handler admits the run.
    response = await scenario.run(endpoint, async_mode)
    # Then allow alone reaches graph execution, and no exception detail leaks.
    assert (
        response.status_code
        == {"allow": 200, "deny": 403, "unauthenticated": 401, "error": 500}[decision]
    )
    assert len(contexts) == 1
    assert len(scenario.calls) == (1 if decision == "allow" else 0)
    assert scenario.lock.attempts == (1 if decision == "allow" else 0)
    assert scenario.observer.rejections == ([] if decision == "allow" else ["authorization_denied"])
    assert b"secret-hook-error" not in response.get_body()
    assert b"secret-principal" not in response.get_body()


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("thread_id", ["thread-one", None])
@pytest.mark.parametrize("hook", [False, True])
async def test_allowed_and_no_hook_runs_preserve_output(
    endpoint: Literal["invoke", "stream"],
    async_mode: bool,
    thread_id: str | None,
    hook: bool,
) -> None:
    # Given a permissive policy or the backward-compatible no-hook configuration.
    scenario = Scenario((lambda context: True) if hook else None, thread_id)
    # When executing a real compiled graph.
    response = await scenario.run(endpoint, async_mode)
    # Then JSON/SSE contains meaningful graph output and admission succeeds.
    assert response.status_code == 200
    assert b"executed" in response.get_body()
    assert len(scenario.calls) == 1
    assert scenario.observer.rejections == []


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
async def test_invalid_thread_is_rejected_before_authorization(
    endpoint: Literal["invoke", "stream"],
    async_mode: bool,
) -> None:
    # Given an unsafe thread ID and a policy whose invocation is observable.
    contexts: list[AuthorizationContext] = []

    def authorize(context: AuthorizationContext) -> bool:
        contexts.append(context)
        return True

    scenario = Scenario(authorize, "invalid\nthread")
    # When the request is parsed.
    response = await scenario.run(endpoint, async_mode)
    # Then parsing fails before policy, lock, or graph access.
    assert response.status_code == 400
    assert contexts == []
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
