import logging

import pytest

from azure_functions_langgraph.authorization import AuthorizationAction
from tests.auth_boundary_support import CANARIES, OwnerPolicy, async_handler, make_app, request


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.asyncio
async def test_native_async_rechecks_revocation(
    endpoint: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Given: the owner succeeds on this exact async route before credentials are revoked.
    policy = OwnerPolicy()
    app = make_app(policy, async_mode=True)
    execute = async_handler(app, f"aflg_agent_{endpoint}")
    assert (await execute(request("owner"))).status_code == 200
    policy.active = False
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    # When: the same owner resumes after revocation.
    response = await execute(request("owner"))
    # Then: policy is rechecked and the denial contains no checkpoint or credential canary.
    assert response.status_code == 403
    assert policy.calls[-1].action == AuthorizationAction.RUN
    assert all(c not in response.get_body().decode() + caplog.text for c in CANARIES)


@pytest.mark.parametrize("endpoint", ["invoke", "stream"])
@pytest.mark.asyncio
async def test_native_async_without_callback_is_permissive(endpoint: str) -> None:
    # Given: host-key authentication installs no ownership policy.
    app = make_app(None, async_mode=True)
    # When: a different caller resumes a known checkpoint.
    response = await async_handler(app, f"aflg_agent_{endpoint}")(request("intruder"))
    # Then: compatibility remains permissive, not implicitly tenant-isolated.
    assert response.status_code == 200
    assert CANARIES[1] in response.get_body().decode()
