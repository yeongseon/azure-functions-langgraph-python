from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import azure.functions as func


class AuthorizationAction(StrEnum):
    THREAD_CREATE = "thread:create"
    THREAD_READ = "thread:read"
    THREAD_UPDATE = "thread:update"
    THREAD_DELETE = "thread:delete"
    THREAD_SEARCH = "thread:search"
    RUN = "run"
    STATE_READ = "state:read"
    STATE_UPDATE = "state:update"
    HISTORY_READ = "history:read"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    action: AuthorizationAction
    graph_name: str | None
    thread_id: str | None
    request: func.HttpRequest


class UnauthenticatedError(Exception):
    """Signal that authorization failed because no principal was authenticated."""


class ThreadAuthorizer(Protocol):
    def __call__(self, context: AuthorizationContext) -> bool | Awaitable[bool]: ...


def authorization_status(
    authorize: ThreadAuthorizer | None,
    context: AuthorizationContext,
) -> int | None:
    if authorize is None:
        return None
    try:
        result = authorize(context)
        if isinstance(result, bool):
            allowed = result
        else:

            async def await_result() -> bool:
                return await result

            allowed = asyncio.run(await_result())
    except UnauthenticatedError:
        return 401
    return None if allowed else 403
