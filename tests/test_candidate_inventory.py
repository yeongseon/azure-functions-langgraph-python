from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from tools.audit_candidate import PROFILES, audit
from tools.candidate_inventory import Candidate
from tools.security_report import Status


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("outcome", ["clean", "advisory", "skip", "mismatch", "absent"])
def test_unpublished_candidate_is_verified_before_exclusion(
    tmp_path: Path, profile: str, outcome: str
) -> None:
    # Given an unpublished wheel, when auditing its inventory, then only that candidate is excluded.
    wheel = tmp_path / "unique_candidate-99.0.1-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "unique_candidate-99.0.1.dist-info/METADATA",
            "Name: unique-candidate\nVersion: 99.0.1\n",
        )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "install" in command:
            target = tmp_path / (
                f"runtime/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
            )
            for name, version in (
                ("unique-candidate", "98" if outcome == "mismatch" else "99.0.1"),
                ("idna", "3.11"),
            ):
                if name == "unique-candidate" and outcome == "absent":
                    continue
                metadata = target / f"{name.replace('-', '_')}-{version}.dist-info/METADATA"
                metadata.parent.mkdir(parents=True)
                metadata.write_text(f"Name: {name}\nVersion: {version}\n")
        if "pip_audit" in command:
            requirements = Path(command[command.index("-r") + 1]).read_text()
            assert requirements == "idna==3.11\n"
            package = {"name": "idna", "version": "3.11", "vulns": []}
            if outcome == "skip":
                return subprocess.CompletedProcess(
                    command, 0, '{"dependencies":[{"name":"idna","skip_reason":"not on PyPI"}]}', ""
                )
            if outcome == "advisory":
                return subprocess.CompletedProcess(
                    command,
                    1,
                    '{"dependencies":[{"name":"idna","version":"3.11","vulns":[{"id":"GHSA-abcd-1234-efgh"}]}]}',
                    "",
                )
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"dependencies": [package]}), ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    with patch("tools.audit_candidate.subprocess.run", side_effect=run):
        result = audit(wheel, profile, tmp_path)
    expected = {
        "clean": Status.PASS,
        "advisory": Status.FAIL,
        "skip": Status.INCOMPLETE,
        "mismatch": Status.INCOMPLETE,
        "absent": Status.INCOMPLETE,
    }
    assert result.status == expected[outcome]


@pytest.mark.parametrize("version", ["99.0.1", "98"])
def test_candidate_returned_by_scanner_requires_matching_version(version: str) -> None:
    # Given a published candidate entry, when checking identity, then mismatches cannot pass.
    candidate = Candidate("unique-candidate", "99.0.1")
    raw = json.dumps(
        {
            "dependencies": [
                {"name": "unique-candidate", "version": version, "vulns": []},
                {"name": "idna", "version": "3.11", "vulns": []},
            ]
        }
    )
    if version == "99.0.1":
        candidate.verify_report(raw, ("idna==3.11",))
    else:
        with pytest.raises(ValueError):
            candidate.verify_report(raw, ("idna==3.11",))


def test_freeze_keeps_every_third_party_package(tmp_path: Path) -> None:
    # Given pip and setuptools dependencies, when freezing, then only the candidate is removed.
    for name in ("candidate", "pip", "setuptools"):
        metadata = tmp_path / f"{name}-1.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(f"Name: {name}\nVersion: 1\n")
    assert Candidate("candidate", "1").freeze(tmp_path) == ("pip==1", "setuptools==1")


def test_duplicate_candidate_scanner_entries_are_incomplete() -> None:
    raw = json.dumps(
        {
            "dependencies": [
                {"name": "candidate", "version": "1", "vulns": []},
                {"name": "candidate", "version": "1", "vulns": []},
                {"name": "idna", "version": "3.11", "vulns": []},
            ]
        }
    )
    with pytest.raises(ValueError):
        Candidate("candidate", "1").verify_report(raw, ("idna==3.11",))
