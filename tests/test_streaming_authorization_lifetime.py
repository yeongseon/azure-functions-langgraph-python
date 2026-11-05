"""Admission is separate from deferred ASGI ownership, without policy replay."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING

import anyio
import pytest
from starlette.responses import StreamingResponse

from tests.streaming_auth_support import OwnerPolicy, Scenario, execute_response

if TYPE_CHECKING:
    from azure_functions_langgraph.streaming_authorization import StreamingAuthorizationContext


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("body_only", [False, True])
async def test_authorization_precedes_body_and_is_not_replayed(
    async_mode: bool, body_only: bool
) -> None:
    # Given a checkpointed stream admitted before the transport executes it.
    policy = OwnerPolicy()
    scenario = Scenario(policy.async_call)
    handler = scenario.handler("stream", async_mode)
    response = await handler(scenario.request())
    assert len(policy.calls) == 1
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert scenario.observer.events == []
    policy.active = False
    # When the admitted response is consumed after policy changes.
    if body_only:
        assert isinstance(response, StreamingResponse)
        body = "".join([str(chunk) async for chunk in response.body_iterator]).encode()
    else:
        status, body = await execute_response(response)
        assert status == 200
    # Then admission is not repeated, and deferred ownership still covers the run.
    assert len(policy.calls) == 1
    assert len(scenario.calls) == 1
    assert scenario.lock.attempts == 1
    assert b"executed" in body
    assert b"event: end" in body


async def test_abandoned_authorized_response_never_acquires_resources() -> None:
    # Given an admitted stream that no transport will execute.
    policy = OwnerPolicy()
    scenario = Scenario(policy)
    response = await scenario.handler("stream", False)(scenario.request())
    assert isinstance(response, StreamingResponse)
    assert isinstance(response.body_iterator, AsyncGenerator)
    # When abandoning the unstarted generator.
    async with aclosing(response.body_iterator):
        pass
    # Then admission happened once, but no resource or observer span was started.
    assert len(policy.calls) == 1
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert scenario.observer.events == []


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize(
    "body",
    [
        b'{"input":{},"config":{"callbacks":[]}}',
        b'{"input":{},"config":{"configurable":{"thread_id":"bad\\nthread"}}}',
        b'{"input":{}}',
    ],
)
async def test_config_admission_precedes_authorization(endpoint: str, body: bytes) -> None:
    # Given an unsafe config, invalid thread or missing required checkpoint thread.
    policy = OwnerPolicy()
    scenario = Scenario(policy)
    handler = scenario.handler(endpoint, False)
    # When request validation rejects it.
    status, _ = await execute_response(await handler(scenario.request(body=body)))
    # Then no policy, graph or lock runs on invalid input (#770 remains first).
    assert status == 400
    assert policy.calls == []
    assert scenario.calls == []
    assert scenario.lock.attempts == 0


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_policy_cancellation_never_admits_graph_work(endpoint: str) -> None:
    # Given a callback cancelled before deciding whether admission is allowed.
    async def cancelled(context: StreamingAuthorizationContext) -> bool:
        raise anyio.get_cancelled_exc_class()()

    scenario = Scenario(cancelled)
    handler = scenario.handler(endpoint, False)
    # When cancellation propagates through the route.
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await handler(scenario.request())
    # Then cancellation is not converted into allow or a generic execution error.
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
