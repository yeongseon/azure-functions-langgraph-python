from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
from pathlib import Path
import socket
import sys
import types
from typing import cast

import pytest

from tests.storage_service import fail_or_skip_service, require_tcp_service


def test_unavailable_optional_service_skips_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_connection(address: tuple[str, int], timeout: float) -> None:
        raise ConnectionRefusedError(address)

    monkeypatch.delenv("AZURITE_REQUIRED", raising=False)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)

    with pytest.raises(pytest.skip.Exception, match="Azurite Blob is unavailable"):
        require_tcp_service("127.0.0.1", 10000, "Azurite Blob")


def test_unavailable_required_service_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_connection(address: tuple[str, int], timeout: float) -> None:
        raise ConnectionRefusedError(address)

    monkeypatch.setenv("AZURITE_REQUIRED", "true")
    monkeypatch.setattr(socket, "create_connection", refuse_connection)

    with pytest.raises(pytest.fail.Exception, match="Azurite Blob is unavailable"):
        require_tcp_service("127.0.0.1", 10000, "Azurite Blob")


def test_available_service_closes_preflight_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        closed = False

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: connection)

    require_tcp_service("127.0.0.1", 10000, "Azurite Blob")

    assert connection.closed


def test_unavailable_cosmos_emulator_skips_unless_cosmos_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURITE_REQUIRED", "true")
    monkeypatch.delenv("COSMOS_REQUIRED", raising=False)

    with pytest.raises(pytest.skip.Exception, match="Cosmos emulator not available"):
        fail_or_skip_service("Cosmos emulator not available", required_env="COSMOS_REQUIRED")


def test_unavailable_required_cosmos_emulator_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURITE_REQUIRED", raising=False)
    monkeypatch.setenv("COSMOS_REQUIRED", "true")

    with pytest.raises(BaseException, match="Cosmos emulator not available") as raised:
        fail_or_skip_service("Cosmos emulator not available", required_env="COSMOS_REQUIRED")

    assert raised.type is pytest.fail.Exception


COSMOS_CONFTEST = Path(__file__).parent / "integration" / "conftest.py"
UNREACHABLE = "Cosmos emulator not available at https://emulator.invalid:8081: emulator unreachable"


def _cosmos_fixture(
    monkeypatch: pytest.MonkeyPatch, *, installed: bool
) -> Callable[[], Iterator[object]]:
    """The real cosmos_emulator_target fixture, with an azure.cosmos that cannot connect."""

    class UnreachableClient:
        def __init__(self, **_: object) -> None:
            raise ConnectionError("emulator unreachable")

    cosmos = types.SimpleNamespace(CosmosClient=UnreachableClient, PartitionKey=dict)
    exceptions = types.SimpleNamespace(CosmosHttpResponseError=Exception)
    monkeypatch.setitem(sys.modules, "azure.cosmos", cosmos if installed else None)
    monkeypatch.setitem(sys.modules, "azure.cosmos.exceptions", exceptions)
    monkeypatch.setenv("COSMOS_EMULATOR_ENDPOINT", "https://emulator.invalid:8081")
    spec = importlib.util.spec_from_file_location("cosmos_integration_conftest", COSMOS_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Callable[[], Iterator[object]], module.cosmos_emulator_target.__wrapped__)


@pytest.mark.parametrize(
    ("installed", "message"),
    [
        pytest.param(True, UNREACHABLE, id="unreachable-emulator"),
        pytest.param(False, "Cosmos dependencies unavailable", id="missing-package"),
    ],
)
def test_cosmos_fixture_skips_when_cosmos_is_not_required(
    monkeypatch: pytest.MonkeyPatch, installed: bool, message: str
) -> None:
    fixture = _cosmos_fixture(monkeypatch, installed=installed)
    monkeypatch.delenv("COSMOS_REQUIRED", raising=False)

    with pytest.raises(pytest.skip.Exception, match=message):
        next(fixture())


@pytest.mark.parametrize(
    ("installed", "message"),
    [
        pytest.param(True, UNREACHABLE, id="unreachable-emulator"),
        pytest.param(False, "Cosmos dependencies unavailable", id="missing-package"),
    ],
)
def test_cosmos_fixture_errors_instead_of_skipping_when_cosmos_is_required(
    monkeypatch: pytest.MonkeyPatch, installed: bool, message: str
) -> None:
    fixture = _cosmos_fixture(monkeypatch, installed=installed)
    monkeypatch.setenv("COSMOS_REQUIRED", "true")

    with pytest.raises(BaseException, match=message) as raised:
        next(fixture())

    assert raised.type is pytest.fail.Exception
