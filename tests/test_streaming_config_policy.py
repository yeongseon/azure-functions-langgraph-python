"""Configuration admission parity, exercised through compiled graphs and ASGI."""

import json
import logging
from typing import Literal

import azure.functions as func
import pytest
from starlette.requests import Request
from starlette.types import Message

from azure_functions_langgraph.streaming import StreamingLangGraphApp
from tests.test_native_run_authorization import Scenario


async def streaming_response(
    scenario: Scenario, endpoint: Literal["invoke", "stream"], async_mode: bool
) -> tuple[int, bytes]:
    """Run registered extension routes and send the entire response over ASGI."""
    app = StreamingLangGraphApp(thread_lock=scenario.lock, observer=scenario.observer)
    reg = scenario.app._registrations["agent"]
    app.register(
        reg.graph,
        "agent",
        async_mode=async_mode,
        allowed_configurable_keys=reg.allowed_configurable_keys,
    )
    handlers = {
        function.get_function_name(): function.get_user_function()
        for function in app.function_app.get_functions()
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": scenario.request.get_body()}

    request = Request({"type": "http", "headers": []}, receive)
    response = await handlers[f"aflg_agent_{endpoint}"](request)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return messages[0]["status"], b"".join(message.get("body", b"") for message in messages)


@pytest.mark.parametrize("transport", ["classic", "streaming"])
@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize(
    ("key", "top_level", "detail"),
    [
        ("checkpoint_ns", False, "Unapproved config.configurable field"),
        ("checkpoint_id", False, "Unapproved config.configurable field"),
        ("recursion_limit", True, "Only config.configurable is client configurable"),
        ("callbacks", True, "Only config.configurable is client configurable"),
        ("unknown", False, "Unapproved config.configurable field"),
        ("__internal", False, "Internal configurable keys are not permitted"),
    ],
)
async def test_denied_config_precedes_execution_on_every_transport(
    transport: str,
    endpoint: Literal["invoke", "stream"],
    async_mode: bool,
    key: str,
    top_level: bool,
    detail: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a valid checkpointed graph and an untrusted execution control.
    scenario = Scenario(None, "thread-one")
    config = {"configurable": {"thread_id": "thread-one"}}
    if top_level:
        payload = {**config, key: "config-admission-canary"}
    else:
        payload = {"configurable": {**config["configurable"], key: "config-admission-canary"}}
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps({"input": {"value": "input-canary"}, "config": payload}).encode(),
    )
    caplog.set_level(logging.DEBUG)
    # When the complete native/ASGI response executes, including deferred bodies.
    if transport == "streaming":
        status, body = await streaming_response(scenario, endpoint, async_mode)
    else:
        response = await scenario.run(endpoint, async_mode)
        status, body = response.status_code, response.get_body()
    # Then the same sanitized 400 precedes all lock, graph and observer start work.
    assert status == 400
    assert json.loads(body)["detail"] == detail
    assert scenario.lock.attempts == 0
    assert scenario.calls == []
    assert scenario.observer.started == 0
    assert scenario.observer.rejections == ["invalid_request"]
    assert b"config-admission-canary" not in body
    assert "config-admission-canary" not in caplog.text
    assert "input-canary" not in caplog.text


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("key", ["locale", "__internal", "callbacks"])
async def test_streaming_opt_in_is_narrow_and_preserves_thread_execution(
    endpoint: Literal["invoke", "stream"], async_mode: bool, key: str
) -> None:
    # Given server opt-in to a name, which must never allow internal/top-level controls.
    scenario = Scenario(None, "thread-one")
    reg = scenario.app._registrations.pop("agent")
    scenario.app.register(reg.graph, "agent", allowed_configurable_keys=frozenset({key}))
    config = {"configurable": {"thread_id": "thread-one", key: "en"}}
    payload = (
        {"configurable": {"thread_id": "thread-one"}, key: []} if key == "callbacks" else config
    )
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps({"input": {"value": "input"}, "config": payload}).encode(),
    )
    # When the ASGI transport sends the response rather than inspecting its type.
    status, body = await streaming_response(scenario, endpoint, async_mode)
    # Then only the opted-in application field executes, with the original thread lock.
    assert status == (200 if key == "locale" else 400)
    assert scenario.lock.attempts == (1 if key == "locale" else 0)
    assert len(scenario.calls) == (1 if key == "locale" else 0)
    assert scenario.observer.started == (1 if key == "locale" else 0)
    if key == "locale":
        assert b"executed" in body


@pytest.mark.parametrize("async_mode", [False, True])
async def test_registration_snapshots_opt_in_keys(async_mode: bool) -> None:
    # Given a mutable application allow-list at registration time.
    scenario = Scenario(None, "thread-one")
    app = StreamingLangGraphApp()
    keys = {"locale"}
    app.register(
        scenario.app._registrations["agent"].graph,
        "agent",
        async_mode=async_mode,
        allowed_configurable_keys=keys,
    )
    # When application code later changes its original collection.
    keys.add("checkpoint_ns")
    # Then the registration's policy remains immutable and narrowly scoped.
    assert app._registrations["agent"].allowed_configurable_keys == frozenset({"locale"})
