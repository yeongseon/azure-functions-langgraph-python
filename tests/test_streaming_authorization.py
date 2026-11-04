"""True-stream RUN admission must precede locks, checkpoints and response bodies."""

from __future__ import annotations

from contextlib import ExitStack
import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from azure_functions_langgraph.streaming import StreamingLangGraphApp
from tests.streaming_auth_support import (
    CANARIES,
    CREDENTIAL,
    SECRET,
    THREAD,
    OwnerPolicy,
    Scenario,
    execute_response,
)

if TYPE_CHECKING:
    from azure_functions_langgraph.streaming_authorization import StreamingAuthorizationContext


def test_constructor_accepts_authorization_opt_in() -> None:
    # Given an application-owned policy.
    policy = OwnerPolicy()
    # When opting in at construction.
    app = StreamingLangGraphApp(authorize=policy)
    # Then the public callback is retained without changing other defaults.
    assert app.authorize is policy


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("async_hook", [False, True])
@pytest.mark.parametrize("threadless", [False, True])
@pytest.mark.parametrize(("credential", "status"), [("", 401), ("fake-intruder", 403)])
async def test_denial_precedes_all_work_and_is_secret_safe(
    endpoint: str,
    async_mode: bool,
    async_hook: bool,
    threadless: bool,
    credential: str,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given actual private checkpoints and forged identity claims beside a missing
    # or cross-user credential. Spies observe real saver methods, not fake results.
    policy = OwnerPolicy()
    scenario = Scenario(policy.async_call if async_hook else policy, threadless=threadless)
    handler = scenario.handler(endpoint, async_mode)
    request = scenario.request(credential)
    caplog.set_level(logging.DEBUG)
    with ExitStack() as stack:
        accesses = [
            stack.enter_context(
                patch.object(scenario.saver, name, wraps=getattr(scenario.saver, name))
            )
            for name in ("get_tuple", "aget_tuple", "put", "aput", "put_writes", "aput_writes")
        ]
        # When both the registered handler and returned ASGI response execute.
        response = await handler(request)
        actual_status, body = await execute_response(response)
        # Then denial is JSON before any checkpoint I/O, graph work or acquisition.
        assert actual_status == status
        assert json.loads(body) == {
            "error": "error",
            "detail": "Access denied",
            "request_id": "auth-test",
        }
        assert all(access.call_count == 0 for access in accesses)
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert scenario.observer.events == ["authorization_denied"]
    assert scenario.observer.rejected[0].thread_id is None
    assert len(policy.calls) == 1
    assert policy.calls[0].request is request
    assert policy.calls[0].thread_id == scenario.thread_id
    evidence = body.decode() + str(response.headers) + caplog.text
    evidence += repr([record.__dict__ for record in caplog.records])
    assert all(canary not in evidence for canary in CANARIES)


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("async_hook", [False, True])
async def test_callback_exception_fails_closed_without_exception_logging(
    endpoint: str, async_mode: bool, async_hook: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a buggy policy whose error contains fake credential/ownership material.
    calls: list[StreamingAuthorizationContext] = []

    def broken(context: StreamingAuthorizationContext) -> bool:
        calls.append(context)
        raise RuntimeError(" ".join(CANARIES))

    async def async_broken(context: StreamingAuthorizationContext) -> bool:
        return broken(context)

    scenario = Scenario(async_broken if async_hook else broken)
    handler = scenario.handler(endpoint, async_mode)
    caplog.set_level(logging.DEBUG)
    # When callback invocation/await fails.
    response = await handler(scenario.request())
    status, body = await execute_response(response)
    # Then fail closed without reflecting exception text, traceback or thread ID.
    assert status == 500
    assert json.loads(body)["detail"] == "Access denied"
    assert len(calls) == 1
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
    assert scenario.observer.events == ["authorization_denied"]
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    evidence = body.decode() + repr([record.__dict__ for record in caplog.records])
    assert all(canary not in evidence for canary in CANARIES)


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("threadless", [False, True])
@pytest.mark.parametrize("hook", [False, True])
async def test_owner_and_missing_hook_preserve_real_output(
    endpoint: str, async_mode: bool, threadless: bool, hook: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # Given an owner policy or the documented permissive default.
    policy = OwnerPolicy()
    scenario = Scenario(policy if hook else None, threadless=threadless)
    handler = scenario.handler(endpoint, async_mode)
    caplog.set_level(logging.DEBUG)
    # When the owner (or cross-user caller with no hook) executes.
    status, body = await execute_response(
        await handler(scenario.request(CREDENTIAL if hook else "fake-intruder"))
    )
    # Then meaningful graph output survives and state is resumed only when threaded.
    assert status == 200
    assert b"executed" in body
    assert (SECRET.encode() in body) is (not threadless)
    assert len(scenario.calls) == 1
    assert scenario.lock.attempts == (0 if threadless else 1)
    evidence = body.decode() + repr([record.__dict__ for record in caplog.records])
    assert all(token not in evidence for token in scenario.lock.tokens)
    assert len(policy.calls) == (1 if hook else 0)
    if endpoint == "stream":
        assert b"event: end" in body
        assert b"event: error" not in body


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
async def test_revocation_is_rechecked_on_subsequent_request(
    endpoint: str, async_mode: bool
) -> None:
    # Given a successful owner run, then revocation in application-owned storage.
    policy = OwnerPolicy()
    scenario = Scenario(policy)
    handler = scenario.handler(endpoint, async_mode)
    assert (await execute_response(await handler(scenario.request())))[0] == 200
    policy.active = False
    # When the same owner requests another run.
    status, body = await execute_response(await handler(scenario.request()))
    # Then fresh admission denies without another execution or lock attempt.
    assert status == 403
    assert len(policy.calls) == 2
    assert len(scenario.calls) == 1
    assert scenario.lock.attempts == 1
    assert THREAD.encode() not in body


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
async def test_graph_entitlement_is_required_even_for_thread_owner(endpoint: str) -> None:
    # Given an authenticated thread owner without entitlement to this graph.
    scenario = Scenario(OwnerPolicy(entitled=False))
    handler = scenario.handler(endpoint, False)
    # When requesting either native execution route.
    status, _ = await execute_response(await handler(scenario.request()))
    # Then ownership alone grants no graph execution.
    assert status == 403
    assert scenario.calls == []
    assert scenario.lock.attempts == 0
