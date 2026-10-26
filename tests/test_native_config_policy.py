"""Client configuration is admitted before authorization, locking or execution."""

import json
from typing import Literal

import azure.functions as func
import pytest

from azure_functions_langgraph.authorization import AuthorizationContext
from tests.test_native_run_authorization import Scenario


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize(
    "config",
    [
        {"configurable": {"checkpoint_id": "old-checkpoint"}},
        {"configurable": {"checkpoint_ns": "other-namespace"}},
        {"configurable": {"tenant_id": "other-tenant"}},
        {"configurable": {"__pregel_send": "hostile"}},
        {"recursion_limit": 1000000},
        {"callbacks": []},
        {"unknown": "secret-value"},
    ],
)
async def test_unapproved_config_never_reaches_admission(
    endpoint: Literal["invoke", "stream"], async_mode: bool, config: dict[str, dict[str, str]]
) -> None:
    # Given an otherwise valid checkpointed run and a permissive authorizer.
    contexts: list[AuthorizationContext] = []

    def authorize(context: AuthorizationContext) -> bool:
        contexts.append(context)
        return True

    scenario = Scenario(authorize, "thread-one")
    config = {
        **config,
        "configurable": {**config.get("configurable", {}), "thread_id": "thread-one"},
    }
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps({"input": {"value": "input"}, "config": config}).encode(),
    )
    # When any native handler parses an unapproved control.
    response = await scenario.run(endpoint, async_mode)
    # Then no policy, lock or graph work occurs and values are not reflected.
    assert response.status_code == 400
    assert b"secret-value" not in response.get_body()
    assert contexts == []
    assert scenario.lock.attempts == 0
    assert scenario.calls == []


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
async def test_explicit_configurable_opt_in_preserves_authorized_run(
    endpoint: Literal["invoke", "stream"],
    async_mode: bool,
) -> None:
    # Given a server-approved application key (not an execution control).
    scenario = Scenario(lambda context: context.thread_id == "thread-one", "thread-one")
    graph = scenario.app._registrations["agent"].graph
    scenario.app._registrations.clear()
    scenario.app.register(graph, "agent", allowed_configurable_keys=frozenset({"locale"}))
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps(
            {
                "input": {"value": "input"},
                "config": {
                    "configurable": {"thread_id": "thread-one", "locale": "ko"},
                },
            }
        ).encode(),
    )
    # When invoking through the configured native path.
    response = await scenario.run(endpoint, async_mode)
    # Then the trusted use case retains authorization and execution.
    assert response.status_code == 200
    assert len(scenario.calls) == 1
    assert scenario.lock.attempts == 1
