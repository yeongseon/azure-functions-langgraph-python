from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import http.client

import pytest

from tools.release_audit_http import HTTPFetcher
from tools.release_audit_model import AuditError


class Wire:
    def __init__(self) -> None:
        self.status = 200
        self.blocks = iter([b"{}", b""])
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.connections: list[str] = []
        self.error = False
        self.sock = None

    def connect(self, host: str, *, timeout: float) -> Wire:
        self.connections.append(host)
        if self.error:
            raise OSError("secret-sentinel /private/path api.github.com")
        return self

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, path, headers))

    @contextmanager
    def getresponse(self) -> Iterator[Wire]:
        yield self

    def read1(self, size: int) -> bytes:
        return next(self.blocks)

    def close(self) -> None:
        self.blocks = iter(())


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    fake = Wire()
    monkeypatch.setattr(http.client, "HTTPSConnection", fake.connect)
    return fake


@pytest.mark.parametrize("host,path", [("evil.example", "/"), ("pypi.org", "relative")])
def test_invalid_destination_never_connects(wire: Wire, host: str, path: str) -> None:
    # Given: a destination outside the fixed HTTPS boundary.
    client = HTTPFetcher()
    # When / Then: refusal happens before a connection is opened.
    with pytest.raises(AuditError):
        client.get(host, path)
    assert wire.connections == []


@pytest.mark.parametrize("status", [301, 302])
def test_transport_rejects_redirect(wire: Wire, status: int) -> None:
    # Given: a redirect from an allowed host.
    wire.status = status
    # When / Then: transport refuses it instead of following or returning evidence.
    with pytest.raises(AuditError):
        HTTPFetcher().get("pypi.org", "/test")
    assert len(wire.connections) == 1


def test_response_size_is_bounded(wire: Wire) -> None:
    # Given: a response exceeding the evidence ceiling.
    wire.blocks = iter([b"x" * (32 * 1024 * 1024), b"x", b""])
    # When / Then: oversized evidence is refused.
    with pytest.raises(AuditError, match="32 MiB"):
        HTTPFetcher().get("pypi.org", "/test")


def test_overall_deadline_is_enforced(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the overall deadline expires during response reading.
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("tools.release_audit_http.time.monotonic", lambda: next(ticks))
    client = HTTPFetcher(deadline_seconds=1)
    # When / Then: per-read socket timeouts cannot extend the overall limit.
    with pytest.raises(AuditError, match="deadline"):
        client.get("pypi.org", "/test")


def test_transport_diagnostics_are_redacted(wire: Wire) -> None:
    # Given: a transport exception with sensitive diagnostics.
    wire.error = True
    # When: collecting through the transport boundary.
    with pytest.raises(AuditError) as error:
        HTTPFetcher().get("api.github.com", "/private/path")
    # Then: neither exception prose nor destination details escape.
    assert str(error.value) == "HTTPS GET failed; diagnostics redacted"


@pytest.mark.parametrize("host", ["api.github.com", "pypi.org"])
def test_authorization_is_host_scoped_on_wire(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    # Given: a supported credential in the environment.
    monkeypatch.setenv("GH_TOKEN", "secret-sentinel")
    # When: performing a GET to each fixed host.
    HTTPFetcher().get(host, "/test")
    # Then: only GitHub receives the token.
    assert ("Authorization" in wire.requests[0][2]) == (host == "api.github.com")


def test_non_utf8_is_collection_error(wire: Wire) -> None:
    # Given: bytes that are not UTF-8 evidence.
    wire.blocks = iter([b"\xff", b""])
    # When / Then: invalid text is refused at collection.
    with pytest.raises(AuditError):
        HTTPFetcher().get("pypi.org", "/test")
