from __future__ import annotations

import importlib


def test_stores_getattr_exports_azure_table_store() -> None:
    stores_module = importlib.import_module("azure_functions_langgraph.stores")
    azure_table_module = importlib.import_module("azure_functions_langgraph.stores.azure_table")

    assert stores_module.AzureTableThreadStore is azure_table_module.AzureTableThreadStore


def test_stores_getattr_unknown_attribute_raises() -> None:
    stores_module = importlib.import_module("azure_functions_langgraph.stores")

    try:
        _ = stores_module.DoesNotExist
    except AttributeError as exc:
        assert "DoesNotExist" in str(exc)
    else:
        raise AssertionError("Expected AttributeError")


def test_checkpointers_getattr_known_exports() -> None:
    checkpointers_module = importlib.import_module("azure_functions_langgraph.checkpointers")
    azure_blob_module = importlib.import_module(
        "azure_functions_langgraph.checkpointers.azure_blob"
    )

    assert (
        checkpointers_module.AzureBlobCheckpointSaver is azure_blob_module.AzureBlobCheckpointSaver
    )
    assert (
        checkpointers_module.OrphanedValueCollectionResult
        is azure_blob_module.OrphanedValueCollectionResult
    )


def test_checkpointers_getattr_unknown_attribute_raises() -> None:
    checkpointers_module = importlib.import_module("azure_functions_langgraph.checkpointers")

    try:
        _ = checkpointers_module.NotExported
    except AttributeError as exc:
        assert "NotExported" in str(exc)
    else:
        raise AssertionError("Expected AttributeError")
