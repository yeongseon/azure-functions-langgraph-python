"""Per-graph authorization policy for the true-streaming route (offline, fake identity).

The tokens and the ownership table below are DEMO data standing in for a real
identity provider and trusted application storage. A real policy must verify a
signed token (or trust an ingress that does) and read ownership from storage
the caller cannot influence.
"""

from __future__ import annotations

from azure_functions_langgraph.authorization import AuthorizationAction, UnauthenticatedError
from azure_functions_langgraph.streaming_authorization import StreamingAuthorizationContext

# Demo credential registry: bearer token -> principal. NOT token verification.
PRINCIPALS = {"Bearer demo-token-alice": "alice", "Bearer demo-token-bob": "bob"}

# Demo ownership store: thread id -> owning principal (server-side, never from the request).
THREAD_OWNERS = {"alice-thread": "alice"}


def authorize(context: StreamingAuthorizationContext) -> bool:
    """Return True to admit, False -> 403; raising UnauthenticatedError -> 401."""
    principal = PRINCIPALS.get(context.request.headers.get("authorization", ""))
    if principal is None:
        raise UnauthenticatedError("missing or unknown credential")
    # Only RUN is evaluated by the streaming hook; be explicit anyway.
    if context.action != AuthorizationAction.RUN or context.graph_name != "guarded_agent":
        return False
    # Fail closed on threadless runs and on threads owned by someone else.
    return context.thread_id is not None and THREAD_OWNERS.get(context.thread_id) == principal
