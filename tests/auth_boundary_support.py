"""Real checkpointed app with a deliberately small test identity boundary."""

from collections.abc import Awaitable
from dataclasses import dataclass, field
import json
from typing import Final, Protocol, TypedDict

import azure.functions as func
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from azure_functions_langgraph import LangGraphApp
from azure_functions_langgraph.authorization import AuthorizationContext, UnauthenticatedError
from azure_functions_langgraph.platform.stores import InMemoryThreadStore

CANARIES: Final = ("tenant-b-thread-canary", "private-state-canary", "credential-canary")


class SecretState(TypedDict):
    secret: str


class Handler(Protocol):
    def __call__(self, request: func.HttpRequest) -> func.HttpResponse: ...


class AsyncHandler(Protocol):
    def __call__(self, request: func.HttpRequest) -> Awaitable[func.HttpResponse]: ...


@dataclass(slots=True)
class OwnerPolicy:
    """Mutable revocation state models credentials being revoked between requests.

    The fake header is trusted ONLY in this offline fixture, never in production.
    """

    active: bool = True
    calls: list[AuthorizationContext] = field(default_factory=list)

    def __call__(self, context: AuthorizationContext) -> bool:
        self.calls.append(context)
        principal = context.request.headers.get("x-test-principal")
        if not principal:
            raise UnauthenticatedError(CANARIES[2])
        return self.active and principal == "owner" and context.thread_id == CANARIES[0]


def make_app(policy: OwnerPolicy | None) -> LangGraphApp:
    def echo(state: SecretState) -> SecretState:
        return state

    builder = StateGraph(SecretState)
    builder.add_node("echo", echo)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    graph = builder.compile(checkpointer=MemorySaver())
    graph.invoke({"secret": CANARIES[1]}, {"configurable": {"thread_id": CANARIES[0]}})
    app = LangGraphApp(platform_compat=True, authorize=policy)
    app.register(graph, name="agent")
    app.thread_store = InMemoryThreadStore(id_factory=lambda: CANARIES[0])
    app.thread_store.create(metadata={"secret": CANARIES[1]})
    app.thread_store.update(CANARIES[0], assistant_id="agent")
    return app


def handler(app: LangGraphApp, name: str) -> Handler:
    function_app = app.function_app
    function_app.functions_bindings = {}
    for function in function_app.get_functions():
        if function.get_function_name() == name:
            return function.get_user_function()
    raise AssertionError(name)


def async_handler(app: LangGraphApp, name: str) -> AsyncHandler:
    function_app = app.function_app
    function_app.functions_bindings = {}
    for function in function_app.get_functions():
        if function.get_function_name() == name:
            return function.get_user_function()
    raise AssertionError(name)


def request(principal: str, method: str = "POST") -> func.HttpRequest:
    return func.HttpRequest(
        method=method,
        url="http://localhost/api/test",
        headers={"x-test-principal": principal, "Authorization": "Bearer " + CANARIES[2]},
        route_params={"thread_id": CANARIES[0]},
        body=json.dumps(
            {
                "assistant_id": "agent",
                "input": {"secret": CANARIES[1]},
                "config": {"configurable": {"thread_id": CANARIES[0]}},
            }
        ).encode(),
    )
