from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from tests.test_release_audit import NOW, ROOT
from tests.test_release_audit_io import registry_file
from tools.audit_releases import main
from tools.release_audit_collect import collect
from tools.release_audit_http import HTTPFetcher, Response, get_json, pages
from tools.release_audit_model import JSON, AuditError
from tools.release_audit_registry import load_registry


class FakeFetcher:
    def __init__(self, replies: dict[str, Response]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, str]] = []

    def get(self, host: str, path: str, accept: str = "application/vnd.github+json") -> Response:
        self.calls.append((host, path, accept))
        return self.replies[path]


def response(value: JSON, status: int = 200) -> Response:
    return Response(status, json.dumps(value))


def recorded_fetcher() -> FakeFetcher:
    base = "/repos/yeongseon/azure-functions-langgraph-python"
    return FakeFetcher(
        {
            base + "/commits/main": response({"sha": "main"}),
            base + "/tags?per_page=100&page=1": response(
                [
                    {"name": "v0.5.0", "commit": {"sha": "abc"}},
                    {"name": "v0.5.0rc1", "commit": {"sha": "preview"}},
                ]
            ),
            base + "/releases?per_page=100&page=1": response(
                [
                    {
                        "tag_name": "v0.5.0",
                        "html_url": "https://github.com/release",
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-04-06T00:00:00Z",
                    }
                ]
            ),
            base + "/actions/workflows/publish-pypi.yml/runs?per_page=100&page=1": response(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 123,
                            "head_branch": "v0.5.0",
                            "html_url": "https://github.com/run",
                            "status": "completed",
                            "conclusion": "success",
                            "head_sha": "abc",
                            "created_at": "2026-04-06T00:00:00Z",
                        }
                    ],
                }
            ),
            base + "/actions/runs/123/jobs?per_page=100&page=1": response(
                {"total_count": 1, "jobs": [{"name": "build-and-publish", "conclusion": "success"}]}
            ),
            base + "/commits/abc": response(
                {"commit": {"committer": {"date": "2026-04-06T00:00:00Z"}}}
            ),
            base + "/compare/abc...main": response({"status": "ahead"}),
            base + "/contents/src/azure_functions_langgraph/__init__.py?ref=abc": response(
                {"encoding": "base64", "content": "X192ZXJzaW9uX18gPSAiMC41LjAiCg=="}
            ),
            "/pypi/azure-functions-langgraph/json": response(
                {
                    "releases": {
                        "0.5.0": [
                            {
                                "filename": "pkg.whl",
                                "packagetype": "bdist_wheel",
                                "digests": {"sha256": "a" * 64},
                                "upload_time_iso_8601": "2026-04-06T00:00:00Z",
                                "yanked": False,
                            },
                            {
                                "filename": "pkg.tar.gz",
                                "packagetype": "sdist",
                                "digests": {"sha256": "b" * 64},
                                "upload_time_iso_8601": "2026-04-06T00:00:00Z",
                                "yanked": False,
                            },
                        ],
                        "0.5.0rc1": [],
                    }
                }
            ),
            **{
                f"/integrity/azure-functions-langgraph/0.5.0/{name}/provenance": response(
                    {
                        "attestation_bundles": [
                            {
                                "publisher": {
                                    "kind": "GitHub",
                                    "repository": "yeongseon/azure-functions-langgraph",
                                    "workflow": "publish-pypi.yml",
                                    "environment": "pypi",
                                }
                            }
                        ]
                    }
                )
                for name in ("pkg.whl", "pkg.tar.gz")
            },
        }
    )


def test_collects_recorded_service_shapes() -> None:
    # Given: small recorded response shapes, including informational prereleases.
    fetcher = recorded_fetcher()
    # When: collecting through the only network seam.
    result = collect(load_registry(ROOT), fetcher, NOW)
    # Then: tag, run, PyPI file and provenance facts are immutable and complete.
    assert result.main_sha == "main"
    assert len(result.versions) == 1
    assert result.versions[0].tag and result.versions[0].tag.ancestor
    assert result.versions[0].tag.source_version == "0.5.0"
    assert len(result.versions[0].files) == 2
    assert len(result.notes) == 2
    assert all(
        accept == "application/vnd.pypi.integrity.v1+json"
        for host, _, accept in fetcher.calls
        if host == "pypi.org" and "integrity" in _
    )


def test_collection_and_offline_replay_output_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: deterministic collection and explicit snapshot persistence.
    output = tmp_path / "snapshot.json"
    registry = registry_file(tmp_path)
    args = ["--registry", str(registry), "--now", NOW.isoformat(), "--format", "json"]
    # When: collecting once and replaying without any fetcher.
    first_code = main([*args, "--snapshot-out", str(output)], fetcher=recorded_fetcher())
    first = capsys.readouterr().out
    replay_code = main([*args, "--snapshot-in", str(output)])
    replay = capsys.readouterr().out
    # Then: snapshot evidence is sufficient for byte-identical results.
    assert first_code == replay_code == 0
    assert first == replay
    assert "Authorization" not in output.read_text()


@pytest.mark.parametrize("status", [301, 302, 403, 429, 500, 404])
def test_http_errors_fail_closed(status: int) -> None:
    # Given: a non-200 response from a fixed host.
    fetcher = FakeFetcher({"/test": response([], status)})
    # When / Then: errors never become empty successful collections.
    with pytest.raises(
        AuditError, match="rate limit.*GH_TOKEN" if status in {403, 429} else "HTTP"
    ):
        get_json(fetcher, "api.github.com", "/test")


def test_provenance_404_is_explicit_absence() -> None:
    # Given: PyPI has no attestation for a file.
    fetcher = FakeFetcher({"/test": response({}, 404)})
    # When: requesting the one endpoint where 404 is expected.
    value = get_json(fetcher, "pypi.org", "/test", provenance=True)
    # Then: absence stays distinct from a collection error.
    assert value is None


def test_pagination_fetches_next_page() -> None:
    # Given: a full first page followed by a partial page.
    fetcher = FakeFetcher(
        {
            "/tags?per_page=100&page=1": response([{}] * 100),
            "/tags?per_page=100&page=2": response([{}]),
        }
    )
    # When: collecting the bounded list.
    result = pages(fetcher, "/tags")
    # Then: no page is silently dropped.
    assert len(result) == 101
    assert len(fetcher.calls) == 2


def test_pagination_ceiling_fails() -> None:
    # Given: every allowed page is full.
    fetcher = FakeFetcher(
        {f"/tags?per_page=100&page={i}": response([{}] * 100) for i in range(1, 51)}
    )
    # When / Then: an unbounded or truncated inventory cannot pass.
    with pytest.raises(AuditError, match="50 pages"):
        pages(fetcher, "/tags")


def test_malformed_collection_cli_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a malformed success response.
    fetcher = recorded_fetcher()
    path = "/repos/yeongseon/azure-functions-langgraph-python/commits/main"
    fetcher.replies[path] = response([])
    # When: invoking collection.
    code = main(["--registry", str(registry_file(tmp_path))], fetcher=fetcher)
    # Then: malformed evidence is an error, not an audit pass.
    assert code == 2
    assert "object" in capsys.readouterr().err


def test_collector_does_not_swallow_api_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: a rate-limited inventory request.
    fetcher = recorded_fetcher()
    path = "/repos/yeongseon/azure-functions-langgraph-python/tags?per_page=100&page=1"
    fetcher.replies[path] = response([], 403)
    # When: running the audit CLI.
    code = main(["--registry", str(registry_file(tmp_path))], fetcher=fetcher)
    # Then: an API failure cannot look like an empty successful list.
    assert code == 2
    assert "rate limit" in capsys.readouterr().err


def test_token_headers_are_host_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: credentials supplied only through the supported environment variable.
    monkeypatch.setenv("GH_TOKEN", "secret-sentinel")
    client = HTTPFetcher()
    # When: constructing headers for each permitted host.
    github = client.headers("api.github.com", "application/json")
    pypi = client.headers("pypi.org", "application/json")
    # Then: credentials never reach PyPI or diagnostics.
    assert github["Authorization"] == "Bearer secret-sentinel"
    assert "Authorization" not in pypi
    assert "secret-sentinel" not in repr(client)


def test_snapshot_contains_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a token in the process environment.
    monkeypatch.setenv("GH_TOKEN", "secret-sentinel")
    # When: collecting facts using recorded responses.
    result = collect(replace(load_registry(ROOT), exceptions=()), recorded_fetcher(), NOW)
    # Then: persisted facts cannot disclose credentials.
    assert "secret-sentinel" not in json.dumps(asdict(result))
