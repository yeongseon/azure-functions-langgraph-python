"""Regression test for the README's distributed-lock startup snippet (#646).

``ContainerClient.create_container()`` is a create operation, not an
idempotent get-or-create: the Azure Storage SDK raises ``ResourceExistsError``
when the container already exists. A worker cold start after the first
successful initialization (or a second scaled instance) would previously raise
during module import, before ``LangGraphApp`` is even constructed.

This extracts the exact Python code block from README.md and executes it
against fake ``azure.storage.blob``/``azure.core.exceptions`` modules,
covering first startup (container does not exist yet), subsequent startup
(container already exists -- the expected, caught case), and a permissions
failure (must NOT be swallowed).
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "docs" / "adapter-reference.md"


def _extract_readme_snippet() -> str:
    content = README.read_text(encoding="utf-8")
    marker = "container.create_container()"
    idx = content.index(marker)
    start = content.rfind("```python", 0, idx)
    end = content.index("```", idx)
    assert start != -1 and end != -1, "distributed-lock snippet not found in README.md"
    block = content[start:end]
    return block.removeprefix("```python\n")


class FakeResourceExistsError(Exception):
    pass


class FakePermissionError(Exception):
    pass


class FakeContainerClient:
    _next_instance: FakeContainerClient | None = None

    def __init__(self, *, already_exists: bool, raise_on_create: Exception | None) -> None:
        self._already_exists = already_exists
        self._raise_on_create = raise_on_create
        self.create_container_calls = 0

    @classmethod
    def from_connection_string(cls, *args: object, **kwargs: object) -> FakeContainerClient:
        assert cls._next_instance is not None, "test must set FakeContainerClient._next_instance"
        return cls._next_instance

    def create_container(self) -> None:
        self.create_container_calls += 1
        if self._raise_on_create is not None:
            raise self._raise_on_create
        if self._already_exists:
            raise FakeResourceExistsError("ContainerAlreadyExists")

    def get_blob_client(self, blob: str) -> object:
        return object()


def _run_readme_snippet(monkeypatch: pytest.MonkeyPatch, container: FakeContainerClient) -> None:
    for mod in list(sys.modules):
        if mod.startswith("azure.storage.blob") or mod.startswith("azure.core.exceptions"):
            monkeypatch.delitem(sys.modules, mod, raising=False)

    azure_storage_mod = types.ModuleType("azure.storage")
    azure_blob_mod = types.ModuleType("azure.storage.blob")
    monkeypatch.setattr(FakeContainerClient, "_next_instance", container)
    azure_blob_mod.ContainerClient = FakeContainerClient  # type: ignore[attr-defined]

    azure_core_mod = types.ModuleType("azure.core")
    azure_core_exceptions_mod = types.ModuleType("azure.core.exceptions")
    azure_core_exceptions_mod.ResourceExistsError = FakeResourceExistsError  # type: ignore[attr-defined]
    azure_core_exceptions_mod.HttpResponseError = Exception  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage_mod)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_blob_mod)
    monkeypatch.setitem(sys.modules, "azure.core", azure_core_mod)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", azure_core_exceptions_mod)

    import azure.functions as func

    from azure_functions_langgraph import LangGraphApp
    from azure_functions_langgraph.locks import AzureBlobLeaseThreadLock

    namespace = {
        "func": func,
        "ResourceExistsError": FakeResourceExistsError,
        "ContainerClient": azure_blob_mod.ContainerClient,
        "LangGraphApp": LangGraphApp,
        "AzureBlobLeaseThreadLock": AzureBlobLeaseThreadLock,
    }
    exec(compile(_extract_readme_snippet(), str(README), "exec"), namespace)  # noqa: S102


def test_first_startup_creates_container_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    container = FakeContainerClient(already_exists=False, raise_on_create=None)
    _run_readme_snippet(monkeypatch, container)
    assert container.create_container_calls == 1


def test_subsequent_startup_handles_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    container = FakeContainerClient(already_exists=True, raise_on_create=None)
    _run_readme_snippet(monkeypatch, container)  # must not raise
    assert container.create_container_calls == 1


def test_permission_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    container = FakeContainerClient(
        already_exists=False, raise_on_create=FakePermissionError("AuthorizationFailure")
    )
    with pytest.raises(FakePermissionError):
        _run_readme_snippet(monkeypatch, container)
