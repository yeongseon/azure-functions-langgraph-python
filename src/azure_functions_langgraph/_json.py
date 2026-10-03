from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import BaseMessage, message_to_dict


def dumps(value: Any, *, allow_nan: bool = True) -> str:
    return json.dumps(value, default=_default, allow_nan=allow_nan)


def _default(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return message_to_dict(value)
    return str(value)


__all__ = ["dumps"]
