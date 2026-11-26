"""Exercise the opt-in harness through offline HTTP fakes, never Azure."""

from __future__ import annotations

import json

import pytest
import requests

from tests.e2e_reference import test_reference as harness
from tests.e2e_reference._contracts import Runtime


@pytest.mark.parametrize("keyless_status", [200, 302, 403, 500])
def test_runtime_rejects_keyless_outcomes(
    monkeypatch: pytest.MonkeyPatch, keyless_status: int
) -> None:
    # Given an authenticated runtime inventory and an unsafe anonymous outcome.
    target = _runtime_target(monkeypatch, keyless_status)
    # When running the real harness assertion, then only a denial is accepted.
    with pytest.raises(AssertionError, match="runtime authentication status differs"):
        harness.test_deployed_runtime(target)


def test_runtime_accepts_keyless_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a protected runtime route.
    target = _runtime_target(monkeypatch, 401)
    # When running the real harness.
    harness.test_deployed_runtime(target)
    # Then version evidence is retained alongside safe access statuses.
    assert target.runtime is not None
    assert target.runtime.adapter == "0.14.2"
    assert target.observations[0].statuses == (401, 200)


def _runtime_target(monkeypatch: pytest.MonkeyPatch, keyless_status: int) -> harness.Reference:
    monkeypatch.setenv("REF882_BASE_URL", "https://reference.invalid")
    monkeypatch.setenv("REF882_FUNCTION_KEY", "synthetic-key")
    monkeypatch.delenv("REF882_EVIDENCE", raising=False)

    def anonymous(url: str, **kwargs: object) -> requests.Response:
        assert url.endswith("/api/reference/runtime")
        assert kwargs.get("allow_redirects") is False
        assert "headers" not in kwargs
        response = requests.Response()
        response.status_code = keyless_status
        response._content = b"private body must stay unread"
        return response

    def authenticated(method: str, url: str, **kwargs: object) -> requests.Response:
        assert method == "GET" and url.endswith("/api/reference/runtime")
        assert kwargs["headers"] == {"x-functions-key": "synthetic-key"}
        versions = dict.fromkeys(Runtime.model_fields, "1.2.3")
        versions.update(adapter="0.14.2", python="3.11.18")
        versions["process_token"] = "a" * 32
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(versions).encode()
        return response

    monkeypatch.setattr(requests, "get", anonymous)
    monkeypatch.setattr(requests, "request", authenticated)
    return harness.Reference()
