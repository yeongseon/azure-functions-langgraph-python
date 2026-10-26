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


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("key", ["__pregel_send", "__pregel_read", "__checkpoint_id"])
async def test_internal_keys_remain_forbidden_when_explicitly_opted_in(
    endpoint: Literal["invoke", "stream"], async_mode: bool, key: str
) -> None:
    # Given a hostile internal key accidentally included in the server allow-list.
    scenario = Scenario(lambda context: True, "thread-one")
    graph = scenario.app._registrations.pop("agent").graph
    scenario.app.register(graph, "agent", allowed_configurable_keys=frozenset({key}))
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps(
            {"input": {}, "config": {"configurable": {"thread_id": "thread-one", key: "secret"}}}
        ).encode(),
    )
    # When the native route parses the opted-in internal control.
    response = await scenario.run(endpoint, async_mode)
    # Then explicit opt-in cannot bypass the internal boundary.
    assert response.status_code == 400
    assert b"secret" not in response.get_body()
    assert scenario.lock.attempts == 0
    assert scenario.calls == []
    assert scenario.observer.started == 0


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("key", ["recursion_limit", "callbacks", "checkpoint_ns"])
async def test_configurable_opt_in_never_allows_top_level_controls(
    endpoint: Literal["invoke", "stream"], async_mode: bool, key: str
) -> None:
    # Given an allow-list that shares a name with a top-level execution control.
    scenario = Scenario(lambda context: True, "thread-one")
    graph = scenario.app._registrations.pop("agent").graph
    scenario.app.register(graph, "agent", allowed_configurable_keys=frozenset({key}))
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps(
            {"input": {}, "config": {"configurable": {"thread_id": "thread-one"}, key: 999}}
        ).encode(),
    )
    # When the caller places that key outside configurable.
    response = await scenario.run(endpoint, async_mode)
    # Then top-level controls remain server-owned regardless of the allow-list.
    assert response.status_code == 400
    assert scenario.lock.attempts == 0
    assert scenario.calls == []


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("key", ["checkpoint_id", "checkpoint_ns", "locale"])
async def test_opted_in_selection_still_requires_run_authorization(
    endpoint: Literal["invoke", "stream"], async_mode: bool, key: str
) -> None:
    # Given explicit checkpoint/application opt-in but no ownership permission.
    contexts: list[AuthorizationContext] = []

    def authorize(context: AuthorizationContext) -> bool:
        contexts.append(context)
        return False

    scenario = Scenario(authorize, "thread-one")
    graph = scenario.app._registrations.pop("agent").graph
    scenario.app.register(graph, "agent", allowed_configurable_keys=frozenset({key}))
    scenario.request = func.HttpRequest(
        method="POST",
        url="https://example.test/invoke",
        body=json.dumps(
            {"input": {}, "config": {"configurable": {"thread_id": "thread-one", key: "secret"}}}
        ).encode(),
    )
    # When a permitted configurable shape reaches authorization.
    response = await scenario.run(endpoint, async_mode)
    # Then the authorizer receives the full request and denial precedes all I/O.
    assert response.status_code == 403
    assert len(contexts) == 1
    assert contexts[0].request is scenario.request
    assert contexts[0].thread_id == "thread-one"
    assert scenario.lock.attempts == 0
    assert scenario.calls == []
    assert scenario.observer.started == 0
