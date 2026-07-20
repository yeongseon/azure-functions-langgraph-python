"""Typed cross-package metadata contract for the ``langgraph`` namespace.

Toolkit convention (shared across the Azure Functions Python DX Toolkit):
handlers carry an ``_azure_functions_metadata`` dict keyed by a package-owned
*namespace* string, so sibling packages can discover metadata **without
importing this package**.

This module gives the ``"langgraph"`` namespace payload a checked ``TypedDict``
shape plus a single merge helper. The contract is intentionally *replicated*
across toolkit packages (not shared via a runtime dependency); keep the
``_BaseMetadata`` ``version`` field and the merge-without-clobber semantics
identical to the sibling packages.

Ref: https://github.com/yeongseon/azure-functions-langgraph-python/issues/269
"""

from __future__ import annotations

from typing import Any, Callable, TypedDict, cast

#: Convention attribute name shared across all toolkit packages.
METADATA_ATTR = "_azure_functions_metadata"

#: Namespace owned by this package.
NAMESPACE = "langgraph"

#: Schema version for the ``langgraph`` namespace payload.
LANGGRAPH_METADATA_VERSION = 1


class _BaseMetadata(TypedDict):
    """Fields common to every toolkit namespace payload."""

    version: int


class LangGraphMetadata(_BaseMetadata):
    """Shape of ``_azure_functions_metadata["langgraph"]`` (schema version 1)."""

    graph_name: str
    endpoint: str


def set_langgraph_metadata(
    fn: Callable[..., Any],
    payload: LangGraphMetadata,
) -> None:
    """Merge the ``langgraph`` namespace onto ``fn`` without clobbering others.

    Reads any pre-existing convention attribute, merges in ``payload`` under
    the ``langgraph`` namespace, and writes the result back onto ``fn``.
    """
    existing = getattr(fn, METADATA_ATTR, None)
    base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    base[NAMESPACE] = payload
    setattr(fn, METADATA_ATTR, base)


def read_langgraph_metadata(func: Any) -> LangGraphMetadata | None:
    """Return the typed ``langgraph`` namespace payload, or ``None`` if absent."""
    md = getattr(func, METADATA_ATTR, None)
    if isinstance(md, dict):
        entry = md.get(NAMESPACE)
        if isinstance(entry, dict):
            return cast("LangGraphMetadata", entry)
    return None
