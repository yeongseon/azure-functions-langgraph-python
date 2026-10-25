from __future__ import annotations

import azure.functions as func
import pytest

from azure_functions_langgraph._run_authorization import (
    native_authorization_status,
    native_authorization_status_async,
)
from azure_functions_langgraph.authorization import (
    AuthorizationAction,
    AuthorizationContext,
    UnauthenticatedError,
)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("decision", ["allow", "deny", "unauthenticated", "error", "absent"])
async def test_native_policy_status_is_fail_closed(async_mode: bool, decision: str) -> None:
    # Given a policy result or an absent hook at the native boundary.
    context = AuthorizationContext(
        AuthorizationAction.RUN,
        "agent",
        None,
        func.HttpRequest(method="POST", url="https://example.test", body=b""),
    )

    def authorize(context: AuthorizationContext) -> bool:
        if decision == "unauthenticated":
            raise UnauthenticatedError
        if decision == "error":
            raise RuntimeError
        return decision == "allow"

    hook = None if decision == "absent" else authorize
    # When the boundary evaluates admission.
    status = (
        await native_authorization_status_async(hook, context)
        if async_mode
        else native_authorization_status(hook, context)
    )
    # Then only explicit allow or no-hook behavior admits execution.
    assert (
        status
        == {"allow": None, "deny": 403, "unauthenticated": 401, "error": 500, "absent": None}[
            decision
        ]
    )
