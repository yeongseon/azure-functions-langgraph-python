from enum import Enum
from types import ModuleType


class MatchConditions(Enum):
    IfNotModified = 2


class ResourceModifiedError(Exception):
    def __init__(self, message: str, *, status_code: int = 412) -> None:
        super().__init__(message)
        self.status_code = status_code


def install_hint_sdk(core: ModuleType, exceptions: ModuleType) -> ModuleType:
    setattr(core, "MatchConditions", MatchConditions)
    setattr(exceptions, "ResourceModifiedError", ResourceModifiedError)
    setattr(exceptions, "HttpResponseError", ResourceModifiedError)
    return core
