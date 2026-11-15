"""Offline allowed-versus-denied comparison (no network, no LLM key, no Functions host).

Run from this directory:  python demo.py
It drives the registered ``stream`` route handler with synthetic ASGI requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import cast

from function_app import langgraph_app
from graph import EXECUTIONS
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message


def make_request(token: str | None, thread_id: str | None, message: str = "hello") -> Request:
    body = json.dumps(
        {
            "input": {"message": message, "reply": "", "internal_note": ""},
            "config": {"configurable": {"thread_id": thread_id}} if thread_id else {},
            "stream_mode": "updates",
        }
    ).encode()

    async def receive() -> Message:
        return {"type": "http.request", "body": body}

    headers = [(b"x-request-id", b"demo")]
    if token is not None:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    return Request({"type": "http", "headers": headers}, receive)


async def drive(response: Response) -> tuple[int, str]:
    """Run the returned ASGI response to completion and return (status, body)."""
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    async def receive() -> Message:
        raise AssertionError("not polled")

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    body = b"".join(m.get("body", b"") for m in messages)
    return messages[0]["status"], body.decode()


def stream_handler() -> Callable[[Request], Awaitable[Response]]:
    for function in langgraph_app.function_app.get_functions():
        if function.get_function_name() == "aflg_guarded_agent_stream":
            return cast(Callable[[Request], Awaitable[Response]], function.get_user_function())
    raise RuntimeError("stream route not registered")


async def main() -> None:
    handler = stream_handler()
    cases = [
        ("allowed (alice on her own thread)", "demo-token-alice", "alice-thread"),
        ("denied 401 (no credential)", None, "alice-thread"),
        ("denied 403 (bob on alice's thread)", "demo-token-bob", "alice-thread"),
        ("denied 403 (alice, threadless run)", "demo-token-alice", None),
    ]
    for label, token, thread in cases:
        before = len(EXECUTIONS)
        status, body = await drive(await handler(make_request(token, thread)))
        ran = len(EXECUTIONS) - before
        print(f"{label}: status={status} graph_runs={ran}")
        print("  body:", body.strip().replace("\n", " | "))


if __name__ == "__main__":
    asyncio.run(main())
