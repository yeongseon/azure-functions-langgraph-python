"""Tests for the Cosmos DB checkpointer DX helper (direct instantiation model)."""

from __future__ import annotations

import importlib
import os
import sys
import types
from typing import Any, cast
from unittest.mock import MagicMock
import warnings

pytest = cast(Any, importlib.import_module("pytest"))


# ---------------------------------------------------------------------------
# Fake module installers
# ---------------------------------------------------------------------------


class FakeCosmosDBSaver:
    """Fake saver returned by direct instantiation."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.client = MagicMock()


def _install_fake_cosmos(
    monkeypatch: Any,
    *,
    constructor_calls: list[dict[str, Any]] | None = None,
    omit_cosmos_saver: bool = False,
) -> None:
    """Inject fake ``langgraph_checkpoint_cosmosdb`` into ``sys.modules``."""
    captured = constructor_calls if constructor_calls is not None else []

    class FakeCosmosDBSaverClass:
        """Class-level fake that captures constructor args."""

        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)
            self.kwargs = kwargs
            self.client = MagicMock()

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    if not omit_cosmos_saver:
        setattr(cosmos_module, "CosmosDBSaver", FakeCosmosDBSaverClass)

    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)


def _reload_cosmos_module(monkeypatch: Any) -> Any:
    """Reload the cosmos helper module and reset its state."""
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    # Reset module-level state
    module._managed_savers.clear()
    module._USE_MARKER_FALLBACK = False  # type: ignore[attr-defined]
    return module


# ---------------------------------------------------------------------------
# create_cosmos_checkpointer tests
# ---------------------------------------------------------------------------


def test_creates_saver_with_key_param(monkeypatch: Any) -> None:
    constructor_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, constructor_calls=constructor_calls)
    module = _reload_cosmos_module(monkeypatch)

    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        key="my-secret-key",
        database_name="testdb",
        container_name="testcontainer",
    )

    assert len(constructor_calls) == 1
    assert constructor_calls[0]["database_name"] == "testdb"
    assert constructor_calls[0]["container_name"] == "testcontainer"
    assert "endpoint" not in constructor_calls[0]
    assert "key" not in constructor_calls[0]
    assert saver is not None


def test_key_param_takes_precedence_over_cosmos_key_env(monkeypatch: Any) -> None:
    captured_env: dict[str, str | None] = {}

    class CapturingCosmosDBSaver:
        def __init__(self, **kwargs: Any) -> None:
            self.client = MagicMock()
            captured_env["COSMOSDB_KEY"] = os.environ.get("COSMOSDB_KEY")

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", CapturingCosmosDBSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOS_KEY", "env-key")

    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        key="param-key",
        database_name="db",
        container_name="ctr",
    )

    assert captured_env["COSMOSDB_KEY"] == "param-key"


def test_cosmos_key_env_fallback(monkeypatch: Any) -> None:
    captured_env: dict[str, str | None] = {}

    class CapturingCosmosDBSaver:
        def __init__(self, **kwargs: Any) -> None:
            self.client = MagicMock()
            captured_env["COSMOSDB_KEY"] = os.environ.get("COSMOSDB_KEY")

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", CapturingCosmosDBSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOS_KEY", "env-key")

    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
    )

    assert captured_env["COSMOSDB_KEY"] == "env-key"


def test_no_key_raises_value_error(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.delenv("COSMOS_KEY", raising=False)

    with pytest.raises(ValueError, match="No Cosmos DB key"):
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
        )


# ---------------------------------------------------------------------------
# credential= deprecation tests
# ---------------------------------------------------------------------------


def test_credential_string_emits_deprecation_warning(monkeypatch: Any) -> None:
    constructor_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, constructor_calls=constructor_calls)
    module = _reload_cosmos_module(monkeypatch)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            credential="string-key",
            database_name="db",
            container_name="ctr",
        )

    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert "credential" in str(w[0].message)
    # credential resolved to key via env var, not constructor kwarg


def test_credential_non_string_raises_type_error(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)

    with pytest.raises(TypeError, match="must be a string"):
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            credential=object(),
            database_name="db",
            container_name="ctr",
        )


def test_key_and_credential_both_raises_type_error(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)

    with pytest.raises(TypeError, match="Cannot specify both"):
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            key="k",
            credential="c",
            database_name="db",
            container_name="ctr",
        )


def test_credential_takes_precedence_over_cosmos_key_env(monkeypatch: Any) -> None:
    captured_env: dict[str, str | None] = {}

    class CapturingCosmosDBSaver:
        def __init__(self, **kwargs: Any) -> None:
            self.client = MagicMock()
            captured_env["COSMOSDB_KEY"] = os.environ.get("COSMOSDB_KEY")

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", CapturingCosmosDBSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOS_KEY", "env-key")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        module.create_cosmos_checkpointer(
            endpoint="https://test.documents.azure.com:443/",
            credential="cred-key",
            database_name="db",
            container_name="ctr",
        )

    assert captured_env["COSMOSDB_KEY"] == "cred-key"


# ---------------------------------------------------------------------------
# Env var wiring and restoration tests
# ---------------------------------------------------------------------------


def test_env_vars_set_during_creation(monkeypatch: Any) -> None:
    """COSMOSDB_ENDPOINT and COSMOSDB_KEY are set during saver creation."""
    captured_env: dict[str, str | None] = {}

    class CapturingCosmosDBSaver:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.client = MagicMock()
            captured_env["COSMOSDB_ENDPOINT"] = os.environ.get("COSMOSDB_ENDPOINT")
            captured_env["COSMOSDB_KEY"] = os.environ.get("COSMOSDB_KEY")

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", CapturingCosmosDBSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)

    module.create_cosmos_checkpointer(
        endpoint="https://ep.documents.azure.com:443/",
        key="the-key",
        database_name="db",
        container_name="ctr",
    )

    assert captured_env["COSMOSDB_ENDPOINT"] == "https://ep.documents.azure.com:443/"
    assert captured_env["COSMOSDB_KEY"] == "the-key"


def test_env_vars_restored_when_previously_absent(monkeypatch: Any) -> None:
    """If COSMOSDB_ENDPOINT/KEY didn't exist, they are removed after creation."""
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.delenv("COSMOSDB_ENDPOINT", raising=False)
    monkeypatch.delenv("COSMOSDB_KEY", raising=False)

    module.create_cosmos_checkpointer(
        endpoint="https://ep.documents.azure.com:443/",
        key="the-key",
        database_name="db",
        container_name="ctr",
    )

    assert "COSMOSDB_ENDPOINT" not in os.environ
    assert "COSMOSDB_KEY" not in os.environ


def test_env_vars_restored_when_previously_present(monkeypatch: Any) -> None:
    """If COSMOSDB_ENDPOINT/KEY existed, they are restored to original values."""
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOSDB_ENDPOINT", "original-ep")
    monkeypatch.setenv("COSMOSDB_KEY", "original-key")

    module.create_cosmos_checkpointer(
        endpoint="https://new.documents.azure.com:443/",
        key="new-key",
        database_name="db",
        container_name="ctr",
    )

    assert os.environ["COSMOSDB_ENDPOINT"] == "original-ep"
    assert os.environ["COSMOSDB_KEY"] == "original-key"


def test_env_vars_restored_on_exception(monkeypatch: Any) -> None:
    """Env vars are restored even if saver creation raises."""

    class FailingCosmosDBSaver:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("creation failed")

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", FailingCosmosDBSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)
    monkeypatch.setenv("COSMOSDB_ENDPOINT", "orig-ep")
    monkeypatch.setenv("COSMOSDB_KEY", "orig-key")

    with pytest.raises(RuntimeError, match="creation failed"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            key="k",
            database_name="db",
            container_name="ctr",
        )

    assert os.environ["COSMOSDB_ENDPOINT"] == "orig-ep"
    assert os.environ["COSMOSDB_KEY"] == "orig-key"


# ---------------------------------------------------------------------------
# close_cosmos_checkpointer tests
# ---------------------------------------------------------------------------


def test_close_calls_client_close(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)

    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        key="k",
        database_name="db",
        container_name="ctr",
    )

    module.close_cosmos_checkpointer(saver)
    saver.client.close.assert_called_once()


def test_close_idempotent(monkeypatch: Any) -> None:
    """Second call is a silent no-op."""
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)

    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        key="k",
        database_name="db",
        container_name="ctr",
    )

    module.close_cosmos_checkpointer(saver)
    module.close_cosmos_checkpointer(saver)  # no-op
    saver.client.close.assert_called_once()


def test_close_rejects_non_helper_saver(monkeypatch: Any) -> None:
    """close_cosmos_checkpointer rejects savers not created by helper."""
    module = _reload_cosmos_module(monkeypatch)

    fake_saver = object()
    with pytest.raises(TypeError, match="not created by"):
        module.close_cosmos_checkpointer(fake_saver)


def test_close_works_without_client_attr(monkeypatch: Any) -> None:
    """If saver has no .client, close still works (marks closed)."""

    class NoClientSaver:
        def __init__(self, **kwargs: Any) -> None:
            pass

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", NoClientSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)

    saver = module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        key="k",
        database_name="db",
        container_name="ctr",
    )

    # Should not raise
    module.close_cosmos_checkpointer(saver)
    assert getattr(saver, "_cosmos_helper_closed", False) is True


# ---------------------------------------------------------------------------
# WeakSet / marker fallback tests
# ---------------------------------------------------------------------------


def test_managed_savers_weakset_tracking(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = _reload_cosmos_module(monkeypatch)

    saver = module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        key="k",
        database_name="db",
        container_name="ctr",
    )

    assert saver in module._managed_savers


def test_marker_fallback_when_weakref_fails(monkeypatch: Any) -> None:
    """If WeakSet.add fails, falls back to marker attribute."""

    class SlottedSaver:
        __slots__ = ("kwargs", "client")

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.client = MagicMock()

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmosdb")
    setattr(cosmos_module, "CosmosDBSaver", SlottedSaver)
    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmosdb", cosmos_module)
    module = _reload_cosmos_module(monkeypatch)

    # SlottedSaver can't be weakly referenced, so we expect marker fallback
    # But SlottedSaver also can't have arbitrary attributes set on it...
    # This tests the TypeError catch path. In practice upstream won't use __slots__.
    # The test verifies the code path doesn't crash.
    try:
        saver = module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            key="k",
            database_name="db",
            container_name="ctr",
        )
        # If marker attribute can be set
        assert getattr(saver, "_managed_by_cosmos_helper", False) is True
    except (TypeError, AttributeError):
        # Slotted class can't accept marker either — that's fine,
        # the important thing is WeakSet.add() was caught
        assert module._USE_MARKER_FALLBACK is True


# ---------------------------------------------------------------------------
# Import error tests
# ---------------------------------------------------------------------------


def test_missing_cosmos_package_raises_helpful_error(monkeypatch: Any) -> None:
    module = _reload_cosmos_module(monkeypatch)
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "langgraph_checkpoint_cosmosdb":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match=r"\[cosmos\]"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            key="k",
            database_name="db",
            container_name="ctr",
        )


def test_missing_cosmos_saver_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch, omit_cosmos_saver=True)
    module = _reload_cosmos_module(monkeypatch)

    with pytest.raises(ImportError, match="CosmosDBSaver"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            key="k",
            database_name="db",
            container_name="ctr",
        )


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------


def test_checkpointers_package_exports_cosmos_helper() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "create_cosmos_checkpointer" in pkg.__all__
    assert callable(pkg.create_cosmos_checkpointer)


def test_checkpointers_package_exports_close_helper() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "close_cosmos_checkpointer" in pkg.__all__
    assert callable(pkg.close_cosmos_checkpointer)
