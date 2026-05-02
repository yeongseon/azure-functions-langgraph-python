from __future__ import annotations

import importlib
import sys
import types
from typing import Any, cast

pytest = cast(Any, importlib.import_module("pytest"))


# ---------------------------------------------------------------------------
# Fake module installers
# ---------------------------------------------------------------------------


class FakeCosmosDBSaver:
    """Fake saver returned by the context manager's ``__enter__``."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._entered = True


class _FakeContextManager:
    """Fake CM that mimics the upstream ``@contextmanager`` behaviour.

    ``__enter__`` returns a *different* object (``FakeCosmosDBSaver``)
    to match the real upstream contract where ``from_conn_info`` is a
    generator-based ``@contextmanager`` that *yields* the saver.
    """

    def __init__(self, saver: FakeCosmosDBSaver, enter_calls: list[int] | None = None) -> None:
        self._saver = saver
        self._enter_calls = enter_calls

    def __enter__(self) -> FakeCosmosDBSaver:
        if self._enter_calls is not None:
            self._enter_calls.append(1)
        return self._saver

    def __exit__(self, *args: Any) -> None:
        pass

def _install_fake_cosmos(
    monkeypatch: Any,
    *,
    from_conn_info_calls: list[dict[str, Any]] | None = None,
    enter_calls: list[int] | None = None,
    omit_cosmos_saver: bool = False,
) -> None:
    """Inject fake ``langgraph_checkpoint_cosmos`` into ``sys.modules``."""
    captured_conn_info = from_conn_info_calls if from_conn_info_calls is not None else []
    captured_enter = enter_calls if enter_calls is not None else []

    class FakeCosmosDBSaverClass:
        """Class-level fake that provides ``from_conn_info``."""

        @classmethod
        def from_conn_info(
            cls,
            *,
            endpoint: str,
            credential: Any,
            database_name: str,
            container_name: str,
        ) -> _FakeContextManager:
            captured_conn_info.append(
                {
                    "endpoint": endpoint,
                    "credential": credential,
                    "database_name": database_name,
                    "container_name": container_name,
                }
            )
            saver = FakeCosmosDBSaver()
            return _FakeContextManager(saver, captured_enter)

    cosmos_module = types.ModuleType("langgraph_checkpoint_cosmos")
    if not omit_cosmos_saver:
        setattr(cosmos_module, "CosmosDBSaver", FakeCosmosDBSaverClass)

    monkeypatch.setitem(sys.modules, "langgraph_checkpoint_cosmos", cosmos_module)


def _install_fake_azure_identity(
    monkeypatch: Any,
    *,
    credential_calls: list[int] | None = None,
    omit_default_credential: bool = False,
) -> None:
    """Inject fake ``azure.identity`` into ``sys.modules``."""
    captured = credential_calls if credential_calls is not None else []

    class FakeDefaultAzureCredential:
        def __init__(self) -> None:
            captured.append(1)

    azure_module = types.ModuleType("azure")
    identity_module = types.ModuleType("azure.identity")
    if not omit_default_credential:
        setattr(identity_module, "DefaultAzureCredential", FakeDefaultAzureCredential)

    monkeypatch.setitem(sys.modules, "azure", azure_module)
    monkeypatch.setitem(sys.modules, "azure.identity", identity_module)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _patch_python_311(monkeypatch: Any) -> None:
    """Pretend we are on Python 3.11 so the runtime guard passes."""
    cosmos_mod = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    monkeypatch.setattr(cosmos_mod.sys, "version_info", (3, 11, 0))

def test_cosmos_helper_creates_saver_and_enters_context(monkeypatch: Any) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    enter_calls: list[int] = []
    _install_fake_cosmos(
        monkeypatch,
        from_conn_info_calls=conn_info_calls,
        enter_calls=enter_calls,
    )
    _install_fake_azure_identity(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="testdb",
        container_name="testcontainer",
    )

    assert len(conn_info_calls) == 1
    assert conn_info_calls[0]["endpoint"] == "https://test.documents.azure.com:443/"
    assert conn_info_calls[0]["database_name"] == "testdb"
    assert conn_info_calls[0]["container_name"] == "testcontainer"
    assert enter_calls == [1]
    # The returned object must be FakeCosmosDBSaver (the yielded saver),
    # NOT the context manager wrapper.
    assert type(saver).__name__ == "FakeCosmosDBSaver"
    assert saver._entered is True


def test_cosmos_helper_returns_yielded_saver_not_cm(monkeypatch: Any) -> None:
    """Verify that ``__enter__`` return value is captured, not the CM."""
    _install_fake_cosmos(monkeypatch)
    _install_fake_azure_identity(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
    )

    # Must be the yielded FakeCosmosDBSaver, not _FakeContextManager
    assert type(saver).__name__ == "FakeCosmosDBSaver"
    assert type(saver).__name__ != "_FakeContextManager"


def test_cosmos_helper_stashes_cm_on_saver(monkeypatch: Any) -> None:
    """The context manager must be stashed on the saver to prevent GC."""
    _install_fake_cosmos(monkeypatch)
    _install_fake_azure_identity(monkeypatch)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    saver = module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
    )

    assert hasattr(saver, "_langgraph_cm")
    assert type(saver._langgraph_cm).__name__ == "_FakeContextManager"


def test_cosmos_helper_default_credential_creates_default_azure_credential(
    monkeypatch: Any,
) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    credential_calls: list[int] = []
    _install_fake_cosmos(monkeypatch, from_conn_info_calls=conn_info_calls)
    _install_fake_azure_identity(monkeypatch, credential_calls=credential_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
    )

    assert credential_calls == [1]
    assert type(conn_info_calls[0]["credential"]).__name__ == "FakeDefaultAzureCredential"


def test_cosmos_helper_explicit_credential_forwarded(monkeypatch: Any) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, from_conn_info_calls=conn_info_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)

    my_cred = object()
    module.create_cosmos_checkpointer(
        endpoint="https://test.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        credential=my_cred,
    )

    assert conn_info_calls[0]["credential"] is my_cred


def test_cosmos_helper_endpoint_forwarded(monkeypatch: Any) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, from_conn_info_calls=conn_info_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    module.create_cosmos_checkpointer(
        endpoint="https://custom.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        credential=object(),
    )

    assert conn_info_calls[0]["endpoint"] == "https://custom.documents.azure.com:443/"


def test_cosmos_helper_database_name_forwarded(monkeypatch: Any) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, from_conn_info_calls=conn_info_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        database_name="my_database",
        container_name="ctr",
        credential=object(),
    )

    assert conn_info_calls[0]["database_name"] == "my_database"


def test_cosmos_helper_container_name_forwarded(monkeypatch: Any) -> None:
    conn_info_calls: list[dict[str, Any]] = []
    _install_fake_cosmos(monkeypatch, from_conn_info_calls=conn_info_calls)

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)
    module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        database_name="db",
        container_name="my_container",
        credential=object(),
    )

    assert conn_info_calls[0]["container_name"] == "my_container"


def test_cosmos_helper_missing_cosmos_package_raises_helpful_error(monkeypatch: Any) -> None:
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "langgraph_checkpoint_cosmos":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)
    _patch_python_311(monkeypatch)

    with pytest.raises(ImportError, match=r"\[cosmos\]"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
        )


def test_cosmos_helper_missing_cosmos_saver_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch, omit_cosmos_saver=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)

    with pytest.raises(ImportError, match="CosmosDBSaver"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
            credential=object(),
        )


def test_cosmos_helper_missing_azure_identity_raises_helpful_error(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)

    real_import_module = importlib.import_module

    def fake_import_module(name: str) -> Any:
        if name == "azure.identity":
            raise ImportError("missing")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import_module)

    with pytest.raises(ImportError, match="azure-identity"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
        )


def test_cosmos_helper_missing_default_azure_credential_symbol_raises(monkeypatch: Any) -> None:
    _install_fake_cosmos(monkeypatch)
    _install_fake_azure_identity(monkeypatch, omit_default_credential=True)
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)

    with pytest.raises(ImportError, match="DefaultAzureCredential"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
        )


def test_checkpointers_package_exports_cosmos_helper() -> None:
    pkg = importlib.import_module("azure_functions_langgraph.checkpointers")
    assert "create_cosmos_checkpointer" in pkg.__all__
    assert callable(pkg.create_cosmos_checkpointer)


def test_cosmos_helper_python_310_raises_runtime_error(monkeypatch: Any) -> None:
    """On Python < 3.11 the helper must raise RuntimeError immediately."""
    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)

    monkeypatch.setattr(module.sys, "version_info", (3, 10, 0))

    with pytest.raises(RuntimeError, match="Python 3.11"):
        module.create_cosmos_checkpointer(
            endpoint="https://x.documents.azure.com:443/",
            database_name="db",
            container_name="ctr",
            credential=object(),
        )


def test_cosmos_helper_explicit_credential_skips_azure_identity(monkeypatch: Any) -> None:
    """When an explicit credential is provided, azure.identity must not be imported."""
    _install_fake_cosmos(monkeypatch)
    # Do NOT install azure.identity — it should never be touched

    module = importlib.import_module("azure_functions_langgraph.checkpointers.cosmos")
    importlib.reload(module)
    _patch_python_311(monkeypatch)

    real_import_module = importlib.import_module

    def spy_import_module(name: str) -> Any:
        if name == "azure.identity":
            raise AssertionError("azure.identity should not be imported with explicit credential")
        return real_import_module(name)

    monkeypatch.setattr(module.importlib, "import_module", spy_import_module)

    my_cred = object()
    saver = module.create_cosmos_checkpointer(
        endpoint="https://x.documents.azure.com:443/",
        database_name="db",
        container_name="ctr",
        credential=my_cred,
    )

    assert type(saver).__name__ == "FakeCosmosDBSaver"
