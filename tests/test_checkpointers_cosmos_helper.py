from __future__ import annotations

import importlib
import os
import sys
import types
from typing import Any, cast
from unittest.mock import MagicMock

pytest = cast(Any, importlib.import_module("pytest"))


# ---------------------------------------------------------------------------
# Fake module installers
# ---------------------------------------------------------------------------


def _install_fake_cosmos(
    monkeypatch: Any,
    *,
    init_calls: list[dict[str, Any]] | None = None,
    omit_cosmos_saver: bool = False,
) -> None:
    """Inject fake ``langgraph_checkpoint_cosmosdb`` into ``sys.modules``."""
    captured_init = init_calls if init_calls is not None else []

    class FakeCosmosDBSaverClass:
        """Class-level fake that captures constructor calls."""

        def __init__(self, database_name: str, container_name: str) -> None:
            captured_init.append(
                {
                    "database_name": database_name,
                    "container_name": container_name,
                    "endpoint_env": os.environ.get("COSMOSDB_ENDPOINT", ""),
                    "key_env": os.environ.get("COSMOSDB_KEY", ""),
                }
            )
            self.database_name = database_name
            self.container_name = container_name
            self.client = MagicMock()

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    if not omit_cosmos_saver:
        setattr(cosmos_module, "CosmosDBSaver", FakeCosmosDBSaverClass)

    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cosmos_helper_creates_saver_with_explicit_key(monkeypatch: Any) -> None:
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="testdb",
        container_name="testcontainer",
        key="my-master-key",
    )

    assert len(init_calls) == 1
    assert init_calls[0]["endpoint_env"] == "https://test.documents.azure.com:443/"
    assert init_calls[0]["key_env"] == "my-master-key"
    assert init_calls[0]["database_name"] == "testdb"
    assert init_calls[0]["container_name"] == "testcontainer"
    assert saver.database_name == "testdb"


def test_cosmos_helper_restores_env_vars(monkeypatch: Any) -> None:
    """Env vars are restored after construction."""
    _install_fake_cosmos(monkeypatch)
    monkeypatch.delenv("COSMOSDB_ENDPOINT", raising=False)
    monkeypatch.delenv("COSMOSDB_KEY", raising=False)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="k",
    )

    # Env vars should be cleaned up
    assert os.environ.get("COSMOSDB_ENDPOINT") is None
    assert os.environ.get("COSMOSDB_KEY") is None


def test_cosmos_helper_restores_existing_env_vars(monkeypatch: Any) -> None:
    """Pre-existing env vars are preserved."""
    _install_fake_cosmos(monkeypatch)
    monkeypatch.setenv("COSMOSDB_ENDPOINT", "original-endpoint")
    monkeypatch.setenv("COSMOSDB_KEY", "original-key")

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://new.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="new-key",
    )

    assert os.environ.get("COSMOSDB_ENDPOINT") == "original-endpoint"
    assert os.environ.get("COSMOSDB_KEY") == "original-key"


def test_cosmos_helper_credential_string_treated_as_key(monkeypatch: Any) -> None:
    """Back-compat: passing credential=<string> is treated as key."""
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        credential="my-string-key",
    )

    assert init_calls[0]["key_env"] == "my-string-key"


def test_cosmos_helper_credential_non_string_raises_type_error(monkeypatch: Any) -> None:
    """Non-string credential must raise TypeError."""
    _install_fake_cosmos(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)

    with pytest.raises(TypeError, match="master key string"):
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
            credential=object(),
        )


def test_cosmos_helper_env_var_fallback(monkeypatch: Any) -> None:
    """When no key/credential provided, COSMOS_KEY env var is used."""
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)
    monkeypatch.setenv("COSMOS_KEY", "env-key-value")

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
    )

    assert init_calls[0]["key_env"] == "env-key-value"


def test_cosmos_helper_no_key_no_env_raises_value_error(monkeypatch: Any) -> None:
    """When no key provided and COSMOS_KEY is unset, ValueError is raised."""
    _install_fake_cosmos(monkeypatch)
    monkeypatch.delenv("COSMOS_KEY", raising=False)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)

    with pytest.raises(ValueError, match="No Cosmos DB key"):
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
        )


def test_cosmos_helper_endpoint_forwarded(monkeypatch: Any) -> None:
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://custom.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="k",
    )

    assert init_calls[0]["endpoint_env"] == "https://custom.documents.azure.com:443/"


def test_cosmos_helper_database_name_forwarded(monkeypatch: Any) -> None:
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        database_name="my_database",
        container_name="ctr",
        key="k",
    )

    assert init_calls[0]["database_name"] == "my_database"


def test_cosmos_helper_container_name_forwarded(monkeypatch: Any) -> None:
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        database_name="db",
        container_name="my_container",
        key="k",
    )

    assert init_calls[0]["container_name"] == "my_container"


def test_cosmos_helper_missing_cosmos_package_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "langgraph_checkpoint_cosmosdb":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[cosmos\]"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
            key="k",
        )


def test_cosmos_helper_missing_cosmos_saver_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch, omit_cosmos_saver=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)

    with pytest.raises(ImportError, match="CosmosDBSaver"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
            key="k",
        )


def test_checkpointers_package_exports_cosmos_helper() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "create_cosmos_checkpointer" in pkg.__all__
    assert callable(pkg.create_cosmos_checkpointer)


def test_cosmos_helper_key_takes_precedence_over_credential(monkeypatch: Any) -> None:
    """When both key and credential are provided, key wins."""
    init_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, init_calls=init_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="explicit-key",
        credential="should-be-ignored",
    )

    assert init_calls[0]["key_env"] == "explicit-key"


# ---------------------------------------------------------------------------
# close_cosmos_checkpointer tests
# ---------------------------------------------------------------------------


def test_close_cosmos_checkpointer_marks_closed(monkeypatch: Any) -> None:
    """close_cosmos_checkpointer marks the saver as closed."""
    _install_fake_cosmos(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="k",
    )

    assert not getattr(saver, "_langgraph_closed", False)
    module.close_cosmos_checkpointer(saver)
    assert saver._langgraph_closed is True


def test_close_cosmos_checkpointer_idempotent(monkeypatch: Any) -> None:
    """Second call to close_cosmos_checkpointer is a silent no-op."""
    _install_fake_cosmos(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        key="k",
    )

    module.close_cosmos_checkpointer(saver)
    # Second call is a no-op — must not raise
    module.close_cosmos_checkpointer(saver)


def test_checkpointers_package_exports_close_helper() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "close_cosmos_checkpointer" in pkg.__all__
    assert callable(pkg.close_cosmos_checkpointer)
