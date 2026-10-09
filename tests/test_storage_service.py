from __future__ import annotations

import socket

import pytest

from tests.storage_service import require_tcp_service


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
