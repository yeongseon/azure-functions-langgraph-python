"""Synthetic compatibility probes; no SDK client or Azure request is constructed."""

from __future__ import annotations

import os
import sys
import traceback
import types
from typing import Literal, assert_never
import warnings

import pytest

from azure_functions_langgraph.checkpointers import cosmos


class Client:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class Saver:
    """Capture the environment handoff without constructing an SDK client."""

    def __init__(self, *, database_name: str, container_name: str) -> None:
        self.database_name = database_name
        self.container_name = container_name
        self.endpoint = os.environ.get("COSMOSDB_ENDPOINT")
        self.key = os.environ.get("COSMOSDB_KEY")
        self.client = Client()


@pytest.fixture
def synthetic_saver(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(module, "CosmosDBSaver", Saver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", module)
    monkeypatch.delenv("COSMOS_DISABLE_SSL", raising=False)
    monkeypatch.setenv("COSMOSDB_ENDPOINT", "https://original.invalid")
    monkeypatch.setenv("COSMOSDB_KEY", "synthetic-original-key")
    monkeypatch.setenv("COSMOS_KEY", "synthetic-fallback-key")


@pytest.mark.parametrize("source", ["key", "alias", "environment"])
def test_compatibility_lifecycle_keeps_separate_warnings_and_redacts_values(
    synthetic_saver: None,
    caplog: pytest.LogCaptureFixture,
    source: Literal["key", "alias", "environment"],
) -> None:
    # Given: distinct synthetic inputs expose precedence and restoration mistakes.
    endpoint = "https://synthetic-account.invalid"
    key = "synthetic-explicit-key"
    alias = "synthetic-alias-key"
    expected_key = "synthetic-fallback-key"
    expected_warnings = 1
    selected_key: str | None = None
    selected_alias: str | None = None
    match source:
        case "key":
            selected_key = key
            expected_key = key
        case "alias":
            selected_alias = alias
            expected_key = alias
            expected_warnings = 2
        case "environment":
            pass
        case unreachable:
            assert_never(unreachable)

    # When: construct and finish the helper-owned lifecycle.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        saver = cosmos.create_cosmos_checkpointer(
            endpoint=endpoint,
            key=selected_key,
            credential=selected_alias,
            database_name="synthetic-db",
            container_name="synthetic-container",
        )
    with warnings.catch_warnings(record=True) as close_warnings:
        warnings.simplefilter("always")
        cosmos.close_cosmos_checkpointer(saver)
        cosmos.close_cosmos_checkpointer(saver)

    # Then: credentials reach only the fake, warnings omit values, cleanup is silent.
    assert saver.key == expected_key
    assert (saver.database_name, saver.container_name) == ("synthetic-db", "synthetic-container")
    assert saver.endpoint == endpoint
    assert os.environ["COSMOSDB_ENDPOINT"] == "https://original.invalid"
    assert os.environ["COSMOSDB_KEY"] == "synthetic-original-key"
    assert len(caught) == expected_warnings
    assert all(item.category is DeprecationWarning for item in caught)
    assert all(item.filename == __file__ for item in caught)
    diagnostics = "\n".join(str(item.message) for item in caught) + caplog.text
    assert all(value not in diagnostics for value in (endpoint, key, alias, expected_key))
    assert close_warnings == []
    assert saver.client.close_count == 1


def test_direct_saver_is_not_closed_by_helper(synthetic_saver: None) -> None:
    # Given: caller-created savers are outside helper resource ownership.
    saver = Saver(database_name="synthetic-db", container_name="synthetic-container")

    # When / Then: helper refuses ownership and leaves caller cleanup untouched.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(TypeError, match="not created by"):
            cosmos.close_cosmos_checkpointer(saver)
    assert caught == []
    assert saver.client.close_count == 0


def test_backend_exception_chain_is_not_a_redaction_boundary(
    synthetic_saver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: upstream can wrap an SDK error containing a synthetic credential.
    secret = "synthetic-error-key-never-a-real-credential"

    class FailingSaver:
        def __init__(self, *, database_name: str, container_name: str) -> None:
            cause = ValueError(secret)
            raise RuntimeError("synthetic upstream initialization failure") from cause

    module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(module, "CosmosDBSaver", FailingSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", module)

    # When: a backend failure propagates through the compatibility helper.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError) as failure:
            cosmos.create_cosmos_checkpointer(
                endpoint="https://synthetic-account.invalid",
                key=secret,
                database_name="synthetic-db",
                container_name="synthetic-container",
            )

    # Then: warnings redact values, but full traceback redaction is not guaranteed.
    assert all(secret not in str(item.message) for item in caught)
    assert secret not in str(failure.value)
    assert failure.value.__cause__ is not None
    assert secret in "".join(traceback.format_exception(failure.value))
    assert os.environ["COSMOSDB_ENDPOINT"] == "https://original.invalid"
    assert os.environ["COSMOSDB_KEY"] == "synthetic-original-key"
