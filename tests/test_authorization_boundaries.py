"""Production policy boundaries through registered handlers, not helper mocks."""

import json
import logging

import pytest

from azure_functions_langgraph.authorization import AuthorizationAction
from tests.auth_boundary_support import (
    CANARIES,
    OwnerPolicy,
    async_handler,
    handler,
    make_app,
    request,
)

ROUTES = (
    ("aflg_platform_threads_create", AuthorizationAction.THREAD_CREATE),
    ("aflg_platform_threads_get", AuthorizationAction.THREAD_READ),
    ("aflg_platform_threads_update", AuthorizationAction.THREAD_UPDATE),
    ("aflg_platform_threads_delete", AuthorizationAction.THREAD_DELETE),
    ("aflg_platform_threads_search", AuthorizationAction.THREAD_SEARCH),
    ("aflg_platform_threads_count", AuthorizationAction.THREAD_SEARCH),
    ("aflg_platform_threads_state_get", AuthorizationAction.STATE_READ),
    ("aflg_platform_threads_state_update", AuthorizationAction.STATE_UPDATE),
    ("aflg_platform_threads_history", AuthorizationAction.HISTORY_READ),
    ("aflg_platform_runs_wait", AuthorizationAction.RUN),
    ("aflg_platform_runs_stream", AuthorizationAction.RUN),
    ("aflg_platform_runs_wait_threadless", AuthorizationAction.RUN),
    ("aflg_platform_runs_stream_threadless", AuthorizationAction.RUN),
    ("aflg_agent_state", AuthorizationAction.STATE_READ),
    ("aflg_agent_invoke", AuthorizationAction.RUN),
    ("aflg_agent_stream", AuthorizationAction.RUN),
)


@pytest.mark.parametrize(("name", "action"), ROUTES)
@pytest.mark.parametrize(("principal", "status"), [("", 401), ("intruder", 403)])
def test_denied_request_is_secret_safe_before_lookup(
    name: str,
    action: AuthorizationAction,
    principal: str,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a real persisted secret and an explicitly configured owner policy.
    policy = OwnerPolicy()
    app = make_app(policy)
    caplog.set_level(logging.DEBUG)
    # When: an unauthenticated or cross-user request reaches the real handler.
    response = handler(app, name)(request(principal))
    # Then: uniform denial contains no state, resource IDs, or credential/exception canary.
    assert response.status_code == status
    assert policy.calls[-1].action == action
    evidence = response.get_body().decode() + caplog.text
    assert all(canary not in evidence for canary in CANARIES)
    assert app.thread_store.get(CANARIES[0]).metadata == {"secret": CANARIES[1]}


@pytest.mark.parametrize(("name", "action"), ROUTES)
def test_revoked_session_is_denied_on_next_request(
    name: str, action: AuthorizationAction, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: this exact action permits the active owner, including collection access.
    policy = OwnerPolicy()
    app = make_app(policy)
    response = handler(app, name)(request("owner", route=name))
    assert response.status_code in {200, 201, 204}, response.get_body()
    assert policy.calls[-1].action == action
    # Restore independent state after destructive operations, keeping the same credentials.
    app = make_app(policy)
    policy.active = False
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    # When: the same credentials are used after revocation.
    response = handler(app, name)(request("owner"))
    # Then: every configured entry point rechecks policy without leaking prior state.
    assert response.status_code == 403
    assert policy.calls[-1].action == action
    assert all(c not in response.get_body().decode() + caplog.text for c in CANARIES)


@pytest.mark.parametrize(("name", "action"), ROUTES)
def test_missing_callback_allows_cross_user_access_without_warning(
    name: str,
    action: AuthorizationAction,
    recwarn: pytest.WarningsRecorder,
) -> None:
    # Given: FUNCTION host auth does not install a resource-ownership callback.
    app = make_app(None)
    # When: another caller knows a persisted thread ID.
    response = handler(app, name)(request("intruder", route=name))
    # Then: current no-hook behavior is permissive, with no isolation warning.
    assert response.status_code in {200, 201, 204}, response.get_body()
    assert not recwarn.list


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
def test_native_run_denies_cross_user_when_policy_configured(endpoint: str) -> None:
    # Given: a RUN-denying owner policy and checkpointed private state.
    app = make_app(OwnerPolicy())
    # When: another user invokes or streams the native endpoint.
    response = handler(app, f"aflg_agent_{endpoint}")(request("intruder"))
    # Then: #687 is fixed; denial must pass normally, never as an expected failure.
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.parametrize(("principal", "status"), [("", 401), ("intruder", 403), ("owner", 200)])
@pytest.mark.asyncio
async def test_native_async_run_authorizes_every_request(
    endpoint: str, principal: str, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: the same policy on the native async registration.
    policy = OwnerPolicy()
    app = make_app(policy, async_mode=True)
    caplog.set_level(logging.DEBUG)
    # When: another user invokes the async route.
    response = await async_handler(app, f"aflg_agent_{endpoint}")(request(principal))
    # Then: #687 enforcement and the successful owner control are both real assertions.
    assert response.status_code == status
    assert policy.calls[-1].action == AuthorizationAction.RUN
    if status != 200:
        assert all(c not in response.get_body().decode() + caplog.text for c in CANARIES)


def test_owner_state_read_returns_real_checkpoint() -> None:
    # Given: persisted graph state, independent of the thread metadata registry.
    app = make_app(OwnerPolicy())
    # When: the owner reads through the native state handler.
    response = handler(app, "aflg_agent_state")(request("owner", "GET"))
    # Then: the positive control proves the denial tests protect real content.
    assert response.status_code == 200
    assert json.loads(response.get_body())["values"] == {"secret": CANARIES[1]}
