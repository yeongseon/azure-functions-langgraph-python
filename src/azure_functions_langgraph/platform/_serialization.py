from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default, allow_nan=False)


def _default(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return value.model_dump(mode="json")
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


__all__ = ["dumps"]
