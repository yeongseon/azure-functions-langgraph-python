"""Per-write hint preconditions; ETags are transport state, never persisted data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import importlib
from typing import Any


class BlobCheckpointConflictError(RuntimeError):
    """A hint changed during a write; the checkpoint may already be committed.

    Stop overlapping writers and inspect the checkpoint by ID before retrying
    under the thread lock. This detection is not checkpoint fencing.
    """


@dataclass(frozen=True, slots=True)
class HintSnapshot:
    payload: bytes | None
    etag: str | None


def response_etag(response: Mapping[str, Any]) -> str:
    etag = response.get("etag")
    if not isinstance(etag, str) or not etag:
        raise RuntimeError("Blob hint writes require an ETag in the upload response")
    return etag


def download_etag(properties: Any) -> str:
    etag = getattr(properties, "etag", None)
    if not isinstance(etag, str) or not etag:
        raise RuntimeError("Blob hint writes require the download response ETag")
    return etag


def hint_conditions(etag: str) -> dict[str, Any]:
    core = importlib.import_module("azure.core")
    return {"etag": etag, "match_condition": core.MatchConditions.IfNotModified}


def conflict_error() -> BlobCheckpointConflictError:
    return BlobCheckpointConflictError(
        "Checkpoint hint changed during publication. A checkpoint may already be committed; "
        "stop overlapping writers, inspect by checkpoint ID, and recover under the thread lock."
    )
