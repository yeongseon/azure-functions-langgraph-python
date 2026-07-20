"""Tests for the typed cross-package metadata contract (``_metadata``)."""

from __future__ import annotations

from azure_functions_langgraph._metadata import (
    LANGGRAPH_METADATA_VERSION,
    METADATA_ATTR,
    NAMESPACE,
    LangGraphMetadata,
    read_langgraph_metadata,
    set_langgraph_metadata,
)


def _payload() -> LangGraphMetadata:
    return {"version": 1, "graph_name": "agent", "endpoint": "invoke"}


class TestContractConstants:
    def test_attr_name_is_toolkit_convention(self) -> None:
        assert METADATA_ATTR == "_azure_functions_metadata"

    def test_namespace_is_langgraph(self) -> None:
        assert NAMESPACE == "langgraph"

    def test_version_constant(self) -> None:
        assert LANGGRAPH_METADATA_VERSION == 1


class TestSetLangGraphMetadata:
    def test_writes_payload_onto_fn(self) -> None:
        def fn() -> None:
            pass

        set_langgraph_metadata(fn, _payload())
        assert getattr(fn, METADATA_ATTR) == {"langgraph": _payload()}

    def test_seeds_from_existing_namespaces(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, {"db": {"version": 1, "bindings": []}})
        set_langgraph_metadata(fn, _payload())

        meta = getattr(fn, METADATA_ATTR)
        assert meta["db"] == {"version": 1, "bindings": []}
        assert meta["langgraph"] == _payload()

    def test_ignores_non_dict_existing_attr(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, "not-a-dict")
        set_langgraph_metadata(fn, _payload())
        assert getattr(fn, METADATA_ATTR) == {"langgraph": _payload()}


class TestReadLangGraphMetadata:
    def test_returns_payload_when_present(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, {"langgraph": _payload()})
        assert read_langgraph_metadata(fn) == _payload()

    def test_returns_none_when_attr_missing(self) -> None:
        def fn() -> None:
            pass

        assert read_langgraph_metadata(fn) is None

    def test_returns_none_when_attr_not_dict(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, "not-a-dict")
        assert read_langgraph_metadata(fn) is None

    def test_returns_none_when_namespace_absent(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, {"db": {"version": 1}})
        assert read_langgraph_metadata(fn) is None

    def test_returns_none_when_namespace_not_dict(self) -> None:
        def fn() -> None:
            pass

        setattr(fn, METADATA_ATTR, {"langgraph": "not-a-dict"})
        assert read_langgraph_metadata(fn) is None
