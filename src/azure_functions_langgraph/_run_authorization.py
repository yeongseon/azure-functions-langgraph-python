"""Fail-closed policy evaluation at the native execution boundary."""

from __future__ import annotations

import logging

from azure_functions_langgraph.authorization import (
    AuthorizationContext,
    ThreadAuthorizer,
    authorization_status,
    authorization_status_async,
)

logger = logging.getLogger(__name__)


def native_authorization_status(
    authorize: ThreadAuthorizer | None, context: AuthorizationContext
) -> int | None:
    try:
        return authorization_status(authorize, context)
    except Exception:
        logger.error("Native RUN authorization failed")
        return 500


async def native_authorization_status_async(
    authorize: ThreadAuthorizer | None, context: AuthorizationContext
) -> int | None:
    try:
        return await authorization_status_async(authorize, context)
    except Exception:
        logger.error("Native RUN authorization failed")
        return 500
