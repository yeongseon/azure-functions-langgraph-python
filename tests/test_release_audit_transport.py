from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import http.client

import pytest

from tools.release_audit_http import HTTPFetcher, get_json
from tools.release_audit_model import AuditError


class Wire:
    def __init__(self) -> None:
        self.status = 200
        self.blocks = iter([b"{}", b""])
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.connections: list[str] = []
        self.error = False
        self.sock = None
        self.script: list[BaseException | tuple[int, bytes]] = []

    def connect(self, host: str, *, timeout: float) -> Wire:
        self.connections.append(host)
        if self.error:
            raise OSError("secret-sentinel /private/path api.github.com")
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            self.status = step[0]
            self.blocks = iter([step[1], b""])
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


def test_transport_diagnostics_are_redacted(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a transport exception with sensitive diagnostics and a token.
    monkeypatch.setenv("GH_TOKEN", "secret-sentinel")
    wire.error = True
    # When: collecting through the transport boundary.
    with pytest.raises(AuditError) as error:
        HTTPFetcher(sleep=lambda _: None).get("api.github.com", "/private/path")
    # Then: only host, class and attempts escape.
    message = str(error.value)
    assert message == (
        "HTTPS GET failed after 3 attempts (api.github.com, OSError); details redacted"
    )
    assert "secret-sentinel" not in message
    assert "/private/path" not in message
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


def client(sleeps: list[float], deadline_seconds: float = 300) -> HTTPFetcher:
    return HTTPFetcher(deadline_seconds=deadline_seconds, sleep=sleeps.append)


@pytest.mark.parametrize("exc", [OSError("x"), http.client.RemoteDisconnected("x")])
def test_retry_succeeds_after_one_transient_failure(wire: Wire, exc: BaseException) -> None:
    # Given: the first attempt fails transiently.
    wire.script = [exc]
    sleeps: list[float] = []
    # When: fetching.
    response = client(sleeps).get("pypi.org", "/t")
    # Then: a second attempt succeeds after the first backoff.
    assert response.status == 200
    assert len(wire.connections) == 2
    assert sleeps == [1.0]


def test_retry_exhaustion_reports_attempts(wire: Wire) -> None:
    # Given: every attempt fails.
    wire.script = [http.client.IncompleteRead(b"") for _ in range(3)]
    sleeps: list[float] = []
    # When / Then: the last failure names host, class and attempt count.
    with pytest.raises(AuditError) as error:
        client(sleeps).get("pypi.org", "/t")
    assert str(error.value) == (
        "HTTPS GET failed after 3 attempts (pypi.org, IncompleteRead); details redacted"
    )
    assert sleeps == [1.0, 2.0]


def test_persistent_503_is_retried_then_reported_as_before(wire: Wire) -> None:
    # Given: a persistent 503.
    wire.script = [(503, b"{}")] * 3
    sleeps: list[float] = []
    # When / Then: three attempts, then get_json raises the original status error.
    with pytest.raises(AuditError) as error:
        get_json(client(sleeps), "pypi.org", "/t")
    assert str(error.value) == "HTTP 503 from pypi.org; redirects are not followed"
    assert len(wire.connections) == 3


def test_502_then_200_succeeds(wire: Wire) -> None:
    # Given: a 502 followed by success.
    wire.script = [(502, b"{}"), (200, b"{}")]
    # When / Then: the retry returns the success.
    assert client([]).get("pypi.org", "/t").status == 200


@pytest.mark.parametrize("status", [400, 403, 404, 429, 301, 302])
def test_non_transient_status_is_not_retried(wire: Wire, status: int) -> None:
    # Given: a non-transient status.
    wire.script = [(status, b"{}")]
    sleeps: list[float] = []
    # When: fetching.
    try:
        client(sleeps).get("pypi.org", "/t")
    except AuditError:
        pass
    # Then: one attempt and no sleep.
    assert len(wire.connections) == 1
    assert sleeps == []


def test_deadline_bounds_retries(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: too little time remains for a backoff plus another attempt.
    wire.script = [OSError("x")]
    ticks = iter([0.0, 0.0, 9.5])
    monkeypatch.setattr("tools.release_audit_http.time.monotonic", lambda: next(ticks))
    sleeps: list[float] = []
    # When / Then: no sleep and no second attempt.
    with pytest.raises(AuditError, match="after 1 attempt "):
        client(sleeps, deadline_seconds=10).get("pypi.org", "/t")
    assert sleeps == []
    assert len(wire.connections) == 1


def test_authorization_on_every_attempt(wire: Wire, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a credential and a transient failure after the request is sent.
    monkeypatch.setenv("GH_TOKEN", "secret-sentinel")
    wire.script = [(500, b"{}"), (200, b"{}")]
    # When: retrying against GitHub.
    client([]).get("api.github.com", "/t")
    # Then: every attempt carries the token.
    assert len(wire.requests) == 2
    assert all("Authorization" in r[2] for r in wire.requests)


def test_unicode_error_is_not_retried(wire: Wire) -> None:
    # Given: non-UTF-8 evidence.
    wire.blocks = iter([b"\xff", b""])
    sleeps: list[float] = []
    # When / Then: one attempt, new message shape.
    with pytest.raises(AuditError) as error:
        client(sleeps).get("pypi.org", "/t")
    assert str(error.value) == (
        "HTTPS GET failed after 1 attempt (pypi.org, UnicodeDecodeError); details redacted"
    )
    assert sleeps == []


def test_unsafe_exception_class_name_is_not_echoed(wire: Wire) -> None:
    # Given: failures whose class name is not a plain identifier.
    unsafe = type("bad name!", (OSError,), {})
    wire.script = [unsafe("x") for _ in range(3)]
    # When / Then: the class name falls back to a fixed word.
    with pytest.raises(AuditError) as error:
        client([]).get("pypi.org", "/t")
    assert str(error.value) == (
        "HTTPS GET failed after 3 attempts (pypi.org, Exception); details redacted"
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: HTTPFetcher(attempts=0),
        lambda: HTTPFetcher(backoff=()),
        lambda: HTTPFetcher(timeout=0),
        lambda: HTTPFetcher(deadline_seconds=0),
    ],
)
def test_invalid_retry_or_time_limits_are_refused(build: Callable[[], HTTPFetcher]) -> None:
    # Given: limits that cannot bound a request.
    # When / Then: construction refuses them before any connection exists.
    with pytest.raises(AuditError):
        build()


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
