"""Opt-in RUN authorization for the experimental ASGI transport."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
import inspect
import logging
from typing import TYPE_CHECKING, Protocol

from azure_functions_langgraph.authorization import AuthorizationAction, UnauthenticatedError

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, repr=False)
class StreamingAuthorizationContext:
    """RUN target and original ASGI request, not an authenticated principal.

    Applications must verify identity and consult trusted entitlement/ownership
    storage. Headers and client-selected thread IDs alone prove no ownership.
    Request/ownership data is deliberately excluded from the generated repr.
    """

    action: AuthorizationAction
    graph_name: str
    thread_id: str | None
    request: Request


class StreamingThreadAuthorizer(Protocol):
    """Return True to allow, False to deny, or raise UnauthenticatedError.

    Sync callbacks run inline and must be nonblocking; awaitables run on the
    request's event loop. Use an async callback for identity/policy storage I/O.
    """

    def __call__(self, context: StreamingAuthorizationContext) -> bool | Awaitable[bool]: ...


async def _authorization_status(
    authorize: StreamingThreadAuthorizer | None, context: StreamingAuthorizationContext
) -> int | None:
    if authorize is None:
        return None
    try:
        result = authorize(context)
        allowed = await result if inspect.isawaitable(result) else result
    except UnauthenticatedError:
        return 401
    except Exception:  # Policy boundary: fail closed without logging credential-bearing exceptions.
        logger.error("Streaming RUN authorization failed")
        return 500
    return None if allowed is True else 403
