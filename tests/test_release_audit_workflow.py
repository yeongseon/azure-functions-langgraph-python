"""The release-audit workflow must stay read-only and must not become a publisher."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-audit.yml"
PUBLISH = ROOT / ".github" / "workflows" / "publish-pypi.yml"


def load() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text()))


def triggers(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return cast("dict[str, Any]", doc["on"] if "on" in doc else cast("Any", doc)[True])


def test_permissions_are_read_only_and_no_secrets() -> None:
    # Given: the workflow text and document.
    text = WORKFLOW.read_text()
    doc = load()
    # Then: only read scopes exist at every level, and no secret is referenced.
    assert doc["permissions"] == {"contents": "read", "actions": "read"}
    for job in doc["jobs"].values():
        assert "permissions" not in job or set(job["permissions"].values()) <= {"read"}
    assert "secrets." not in text
    assert "RELEASE_PLEASE_TOKEN" not in text
    assert "environment:" not in text
    assert "id-token" not in text


def test_runs_on_pull_request_after_publish_and_on_a_schedule() -> None:
    # Given: the triggers.
    on = triggers(load())
    # Then: the check is exercised in PRs that touch it and after each publish run.
    assert "pull_request" in on and "pull_request_target" not in on
    assert "tools/release_audit_*.py" in on["pull_request"]["paths"]
    assert ".github/release-audit-exceptions.json" in on["pull_request"]["paths"]
    assert on["workflow_run"] == {"workflows": ["Publish to PyPI"], "types": ["completed"]}
    assert "schedule" in on and "workflow_dispatch" in on
    # And: the publish workflow really carries that name.
    assert yaml.safe_load(PUBLISH.read_text())["name"] == "Publish to PyPI"


def test_workflow_cannot_publish_tag_or_edit_releases() -> None:
    # Given: every command in the workflow.
    text = WORKFLOW.read_text()
    # Then: nothing publishes, tags, pushes or calls a mutating API.
    forbidden = (
        r"gh release (create|edit|delete|upload)",
        r"git (push|tag)\b",
        r"twine|hatch publish|pypi-publish",
        r"-X\s*(POST|PUT|PATCH|DELETE)",
        r"gh api\b",
        r"workflow_run\.head_(sha|branch|ref)",
        r"github\.event\.workflow_run\.(head|pull)",
    )
    for pattern in forbidden:
        assert not re.search(pattern, text), pattern
    # And: it reports through the exit status of the read-only audit.
    assert "python -m tools.audit_releases" in text
    assert 'exit "$status"' in text


def test_actions_are_pinned_to_full_commit_shas() -> None:
    # Given: every external action reference.
    refs = re.findall(r"uses:\s*(\S+)(?:\s+#\s*(\S+))?", WORKFLOW.read_text())
    # Then: each is a 40-hex SHA with a version comment.
    assert refs
    for ref, comment in refs:
        assert re.fullmatch(r"[\w.\-]+/[\w.\-]+@[0-9a-f]{40}", ref), ref
        assert comment, ref


@pytest.mark.parametrize("event", ["workflow_run", "schedule"])
def test_default_branch_checkout_is_not_overridden(event: str) -> None:
    # Given: the checkout step used for workflow_run and schedule events.
    steps = load()["jobs"]["audit"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    # Then: no ref from the triggering payload is checked out, and credentials are not kept.
    assert "ref" not in checkout["with"], event
    assert checkout["with"]["persist-credentials"] is False
