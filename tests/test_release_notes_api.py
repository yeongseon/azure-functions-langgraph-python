from __future__ import annotations

import http.client
import json

import pytest

from tools import release_notes as rn


@pytest.mark.parametrize("endpoint", ["release", "pr"])
def test_api_rejects_malformed_record_fields(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an object lacking the endpoint's required fields
    client = rn.GitHubApi("acme/pkg")
    monkeypatch.setattr(client, "_request", lambda *args: [{}])
    # When / Then: schema errors use GitHubError, not crashes or silent omission
    with pytest.raises(rn.GitHubError):
        if endpoint == "release":
            client.releases()
        else:
            client.pr_authors()


@pytest.mark.parametrize("status", [301, 302, 307, 400, 500, 204])
def test_api_rejects_unexpected_http_status(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a fixed-host fake transport returning a non-success status
    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return b"{}"

    class Connection:
        def __init__(self, host: str, timeout: float) -> None:
            assert host == "api.github.com"

        def request(
            self, method: str, path: str, body: str | None, headers: dict[str, str]
        ) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    # When / Then: redirects and unexpected empty successes are errors, never followed
    with pytest.raises(rn.GitHubError, match="HTTP"):
        rn.GitHubApi("acme/pkg")._request("GET", "/test")


@pytest.mark.parametrize("payload", [{}, None, "not a list", ["not an object"]])
def test_api_pages_require_list_of_objects(
    payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a malformed list endpoint
    client = rn.GitHubApi("acme/pkg")
    monkeypatch.setattr(client, "_request", lambda *args: payload)
    # When / Then: malformed evidence fails with the tool's API error
    with pytest.raises(rn.GitHubError):
        client.releases()


@pytest.mark.parametrize("payload", [[], {}, {"commits": {}}, {"commits": [None]}])
def test_compare_requires_valid_objects(payload: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: malformed compare evidence
    client = rn.GitHubApi("acme/pkg")
    monkeypatch.setattr(client, "_get", lambda path: payload)
    # When / Then: never silently interpret missing commits as an empty range
    with pytest.raises(rn.GitHubError):
        client.compare("base", "head")


@pytest.mark.parametrize("compare", [False, True])
def test_pagination_ceiling_fails_closed(compare: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an endpoint returning full pages forever
    client = rn.GitHubApi("acme/pkg")
    calls = 0

    def request(*args: object) -> object:
        nonlocal calls
        calls += 1
        if calls > 50:
            raise AssertionError("pagination exceeded ceiling")
        if compare:
            return {
                "commits": [{"sha": "a" * 40, "commit": {"message": "fix: x"}, "author": None}]
                * 100
            }
        return [
            {
                "tag_name": "v1.0.0",
                "published_at": "2026-01-01",
                "created_at": "2026-01-01",
                "draft": False,
            }
        ] * 100

    monkeypatch.setattr(client, "_request", request)
    # When / Then: bounded work raises instead of truncating or fetching a 51st page
    with pytest.raises(rn.GitHubError, match="page|limit"):
        if compare:
            client.compare("base", "head")
        else:
            client.releases()
    assert calls == 50


def test_ref_path_segments_are_completely_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: refs containing path and query delimiters
    client = rn.GitHubApi("acme/pkg")
    paths: list[str] = []

    def get(path: str) -> object:
        paths.append(path)
        return {"commits": []}

    monkeypatch.setattr(client, "_get", get)
    # When: compare arbitrary refs
    client.compare("base/a?b", "head/c#d")
    # Then: neither ref can change the endpoint structure
    assert paths == ["/repos/acme/pkg/compare/base%2Fa%3Fb...head%2Fc%23d?per_page=100&page=1"]


def test_command_deadline_expires_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the overall command budget has elapsed
    now = [0.0]
    monkeypatch.setattr(rn, "monotonic", lambda: now[0], raising=False)
    client = rn.GitHubApi("acme/pkg")
    now[0] = 121.0

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("expired command opened a connection")

    monkeypatch.setattr(http.client, "HTTPSConnection", forbidden)
    # When / Then: deadline is enforced before network work
    with pytest.raises(rn.GitHubError, match="deadline|120"):
        client._request("GET", "/test")


def test_api_release_metadata_preserves_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a release returned by the API
    client = rn.GitHubApi("acme/pkg")
    data = {
        "id": 101,
        "tag_name": "v1.0.1",
        "body": "old",
        "draft": False,
        "published_at": "2026-02-02",
        "created_at": "2026-02-01",
    }
    monkeypatch.setattr(client, "_request", lambda *args: [data])
    # When: request backup metadata
    metadata = client.release_metadata("v1.0.1")
    # Then: the backup uses API identity rather than inventing one
    assert json.loads(json.dumps(metadata)) == {
        "id": 101,
        "tagName": "v1.0.1",
        "body": "old",
        "publishedAt": "2026-02-02",
    }
