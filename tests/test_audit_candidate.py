from pathlib import Path
import subprocess
from unittest.mock import patch

from tools.audit_candidate import PROFILES, audit
from tools.security_report import Status


def test_audit_profiles_cover_every_runtime_extra() -> None:
    import tomllib

    # Given: the project manifest is the canonical supported-extra inventory.
    with Path("pyproject.toml").open("rb") as stream:
        extras = tomllib.load(stream)["project"]["optional-dependencies"]
    # When: production closures are selected independently of dev/docs tooling.
    expected = {"base", *set(extras) - {"dev", "docs"}}
    # Then: a newly added runtime extra cannot silently escape the gate.
    assert set(PROFILES) == expected


def test_candidate_install_failure_is_not_a_vulnerability(tmp_path: Path) -> None:
    # Given: a failing installer, without logging potentially credential-bearing stderr.
    with patch(
        "tools.audit_candidate.subprocess.run",
        side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1, "", "credential-canary"),
        ],
    ):
        # When: a candidate profile cannot be installed.
        result = audit(tmp_path / "candidate.whl", "cosmos", tmp_path)
    # Then: a distinct tool failure contains no installer diagnostics.
    assert result.status == Status.TOOL_ERROR
    assert "credential-canary" not in repr(result)
