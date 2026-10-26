import sys
import types
from unittest.mock import MagicMock
import warnings

import pytest

from tests.test_checkpointers_cosmos_helper import _install_fake_sdk, _reload_cosmos_module


def test_emulator_replacement_closes_original_not_active_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = MagicMock()

    class Saver:
        def __init__(self, database_name: str, container_name: str) -> None:
            self.client = original

    upstream = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(upstream, "CosmosDBSaver", Saver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", upstream)
    replacement = _install_fake_sdk(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOS_DISABLE_SSL", "true")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        saver = module.create_cosmos_checkpointer(
            endpoint="https://localhost:8081",
            key="test",
            database_name="db",
            container_name="items",
        )
    original.close.assert_called_once()
    replacement.return_value.close.assert_not_called()
    assert saver.client is replacement.return_value
