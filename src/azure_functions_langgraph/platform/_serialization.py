from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default, allow_nan=False)


def _default(value: Any) -> Any:
    # Python-mode dump keeps arbitrary nested values (e.g. ToolMessage.artifact)
    # intact so this encoder's ``str`` fallback still applies to them.
    if isinstance(value, BaseMessage):
        return value.model_dump()
    if isinstance(value, BaseModel):
        return value.model_dump()
    return str(value)


__all__ = ["dumps"]
