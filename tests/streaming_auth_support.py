"""Offline ASGI admission fixture; credentials are deliberately fake, not an IdP."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Final, Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from azure_functions_langgraph.authorization import AuthorizationAction, UnauthenticatedError
from azure_functions_langgraph.observability import (
    LoggingRunObserver,
    RunContext,
    RunRejectedReason,
)
from azure_functions_langgraph.streaming import StreamingLangGraphApp
from tests.test_native_run_authorization import AdmissionLock

if TYPE_CHECKING:
    from azure_functions_langgraph.streaming_authorization import (
        StreamingAuthorizationContext,
        StreamingThreadAuthorizer,
    )

THREAD: Final = "other-user-thread-canary"
SECRET: Final = "checkpoint-content-canary"
CREDENTIAL: Final = "fake-credential-canary"
OWNER_TOKEN: Final = "lock-ownership-canary"
CANARIES: Final = (THREAD, SECRET, CREDENTIAL, OWNER_TOKEN)


class State(TypedDict):
    value: str
    secret: str


class Handler(Protocol):
    def __call__(self, request: Request) -> Awaitable[Response]: ...


class TokenLock(AdmissionLock):
    def __init__(self) -> None:
        super().__init__()
        self.tokens: list[str] = []

    def acquire(self, graph_name: str, thread_id: str, timeout: float = 0.0) -> str | None:
        token = super().acquire(graph_name, thread_id, timeout)
        if token is not None:
            self.tokens.append(token)
        return token


class Observer(LoggingRunObserver):
    """Record lifecycle transitions while exercising the built-in log exporter."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.rejected: list[RunContext] = []

    def on_run_started(self, ctx: RunContext) -> None:
        self.events.append("started")
        super().on_run_started(ctx)

    def on_run_rejected(self, ctx: RunContext, reason: RunRejectedReason) -> None:
        self.events.append(reason)
        self.rejected.append(ctx)
        super().on_run_rejected(ctx, reason)


@dataclass(slots=True)
class OwnerPolicy:
    """Mutable policy models server-side revocation and graph entitlement.

    Only a test credential registry establishes identity, never the caller's
    x-ms-client-principal or input owner claim. This is NOT token verification.
    """

    active: bool = True
    entitled: bool = True
    calls: list[StreamingAuthorizationContext] = field(default_factory=list)

    def __call__(self, context: StreamingAuthorizationContext) -> bool:
        self.calls.append(context)
        principal = {"Bearer " + CREDENTIAL: "owner", "Bearer fake-intruder": "intruder"}.get(
            context.request.headers.get("authorization", "")
        )
        if principal is None:
            raise UnauthenticatedError(CREDENTIAL)
        return (
            self.active
            and self.entitled
            and principal == "owner"
            and context.action == AuthorizationAction.RUN
            and context.graph_name == "agent"
            and context.thread_id in (THREAD, None)
        )

    async def async_call(self, context: StreamingAuthorizationContext) -> bool:
        return self(context)


class Scenario:
    """Persist real private state, then count execution on the registered route."""

    def __init__(
        self, policy: StreamingThreadAuthorizer | None, *, threadless: bool = False
    ) -> None:
        self.calls: list[State] = []
        self.lock = TokenLock()
        self.observer = Observer()
        self.saver = InMemorySaver()
        self.thread_id = None if threadless else THREAD

        def node(state: State) -> State:
            self.calls.append(state)
            return {**state, "value": "executed"}

        builder = StateGraph(State)
        builder.add_node("node", node)
        builder.add_edge(START, "node")
        builder.add_edge("node", END)
        self.graph = builder.compile(checkpointer=None if threadless else self.saver)
        if not threadless:
            self.graph.invoke(
                {"value": "seed", "secret": SECRET}, {"configurable": {"thread_id": THREAD}}
            )
            self.calls.clear()
        self.app = StreamingLangGraphApp(thread_lock=self.lock, observer=self.observer)
        # Assignment lets pre-fix tests demonstrate ignored admission, not just a
        # missing constructor keyword. The constructor has its own regression.
        self.app.authorize = policy

    def handler(self, endpoint: str, async_mode: bool) -> Handler:
        self.app.register(self.graph, "agent", async_mode=async_mode)
        for function in self.app.function_app.get_functions():
            if function.get_function_name() == f"aflg_agent_{endpoint}":
                return function.get_user_function()
        raise AssertionError(endpoint)

    def request(self, credential: str = CREDENTIAL, *, body: bytes | None = None) -> Request:
        payload = (
            body
            or json.dumps(
                {
                    "input": {"value": "input", "owner": "owner"},
                    "config": {"configurable": {"thread_id": self.thread_id}}
                    if self.thread_id is not None
                    else {},
                    "stream_mode": "updates",
                }
            ).encode()
        )

        async def receive() -> Message:
            return {"type": "http.request", "body": payload}

        return Request(
            {
                "type": "http",
                "headers": [
                    (b"authorization", ("Bearer " + credential).encode()),
                    (b"x-ms-client-principal", b"unverified-owner-claim"),
                    (b"x-request-id", b"auth-test"),
                ],
            },
            receive,
        )


async def execute_response(response: Response) -> tuple[int, bytes]:
    """Drive the actual ASGI lifetime, including deferred acquisition and body."""
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        raise AssertionError("ASGI 2.4 does not poll disconnects")

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return messages[0]["status"], b"".join(message.get("body", b"") for message in messages)
