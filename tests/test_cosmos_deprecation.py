from __future__ import annotations

import importlib
import os
import sys
import threading
import types
from typing import Any
from unittest.mock import MagicMock
import warnings

import pytest


def _install_saver(monkeypatch: pytest.MonkeyPatch, saver_class: type[Any]) -> Any:
    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", saver_class)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    return importlib.reload(module)


def test_key_based_helper_warns_without_disclosing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class Saver:
        def __init__(self, **kwargs: Any) -> None:
            self.client = MagicMock()

    module = _install_saver(monkeypatch, Saver)
    secret = "warning-must-not-contain-this-key"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            key=secret,
            database_name="db",
            container_name="ctr",
        )

    assert any(item.category is DeprecationWarning for item in caught)
    assert all(secret not in str(item.message) for item in caught)


def test_unrelated_thread_observes_deprecated_helper_key(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor_entered = threading.Event()
    observer_finished = threading.Event()
    observed_key: list[str | None] = []

    class BlockingSaver:
        def __init__(self, **kwargs: Any) -> None:
            self.client = MagicMock()
            constructor_entered.set()
            assert observer_finished.wait(timeout=2)

    module = _install_saver(monkeypatch, BlockingSaver)

    def observe_process_environment() -> None:
        assert constructor_entered.wait(timeout=2)
        observed_key.append(os.environ.get("COSMOSDB_KEY"))
        observer_finished.set()

    observer = threading.Thread(target=observe_process_environment)
    observer.start()
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        key="process-visible-key",
        database_name="db",
        container_name="ctr",
    )
    observer.join(timeout=2)

    assert observed_key == ["process-visible-key"]
