"""Offline guards for the integration suite's credential-name boundary."""

from __future__ import annotations

import importlib
import inspect
from unittest.mock import Mock

import pytest

from tests.integration import test_checkpoint_conformance as integration


@pytest.mark.parametrize("value", [None, ""])
def test_missing_named_blob_credential_fails_without_emulator(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    # Given an explicit endpoint selector and no usable credential.
    monkeypatch.setenv("BLOB_TEST_CONNECTION_STRING_ENV", "PRIVATE_BLOB_CS")
    monkeypatch.delenv("PRIVATE_BLOB_CS", raising=False)
    if value is not None:
        monkeypatch.setenv("PRIVATE_BLOB_CS", value)
    emulator = Mock(side_effect=AssertionError("must not probe Azurite"))
    monkeypatch.setattr(integration, "require_tcp_service", emulator)
    # When selected, then fail with a value-free message before any emulator probe.
    with pytest.raises(pytest.fail.Exception) as caught:
        integration._blob_connection_string()
    assert str(caught.value) == "named Blob connection-string environment variable is empty"
    emulator.assert_not_called()


def test_explicit_blob_endpoint_never_probes_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real-endpoint request whose SDK constructor fails with sensitive details.
    secret = "private-test-credential"
    monkeypatch.setenv("BLOB_TEST_CONNECTION_STRING_ENV", "PRIVATE_BLOB_CS")
    monkeypatch.setenv("PRIVATE_BLOB_CS", secret)
    emulator = Mock(side_effect=AssertionError("must not probe Azurite"))
    monkeypatch.setattr(integration, "require_tcp_service", emulator)
    service = Mock()
    service.BlobServiceClient.from_connection_string.side_effect = ValueError(secret)
    monkeypatch.setattr(importlib, "import_module", lambda _: service)
    # When constructing the fixture, then expose only the exception class, never its value.
    with pytest.raises(pytest.fail.Exception) as caught:
        next(inspect.unwrap(integration.azurite_container_client)())
    assert str(caught.value) == "real Blob service unavailable (ValueError)"
    assert secret not in str(caught.value)
    service.BlobServiceClient.from_connection_string.assert_called_once_with(secret)
    emulator.assert_not_called()


def test_unset_blob_selector_keeps_azurite(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the default integration configuration.
    monkeypatch.delenv("BLOB_TEST_CONNECTION_STRING_ENV", raising=False)
    emulator = Mock()
    monkeypatch.setattr(integration, "require_tcp_service", emulator)
    # When selected, then retain the original emulator endpoint and availability check.
    assert integration._blob_connection_string() == integration.AZURITE_BLOB_CONNECTION_STRING
    emulator.assert_called_once_with("127.0.0.1", 10000, "Azurite Blob")
