from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from tools.verification_evidence import collect


@pytest.mark.parametrize("lane", ["test", "dependency"])
@pytest.mark.parametrize("profile", ["minimum", "resolved-default"])
@pytest.mark.parametrize(
    ("classname", "name", "message"),
    [
        (
            "tests.test_sqlite_walkthrough",
            "test_sqlite_walkthrough_matches_live_handler_output",
            "SQLite extra is unavailable or incompatible: SQLite checkpointer requires the "
            "'sqlite' extra: pip install azure-functions-langgraph[sqlite]",
        ),
        (
            "tests.test_streaming_app.TestAiterGraphEvents",
            "test_sync_mode_uses_stream_with_sqlite_saver",
            "installed SQLite saver is incompatible: cannot import name 'DeltaChannelHistory' "
            "from 'langgraph.checkpoint.base' "
            "(/venv/site-packages/langgraph/checkpoint/base/__init__.py)",
        ),
        (
            "tests.test_checkpointers_azure_blob",
            "test_conformance_base_capabilities",
            "langgraph-checkpoint-conformance not installed",
        ),
    ],
)
def test_minimum_skip_requires_known_identity_and_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    profile: str,
    classname: str,
    name: str,
    message: str,
) -> None:
    # Given a real passing case plus one known dependency-specific skip.
    directory = tmp_path / lane
    directory.mkdir()
    suite = ET.Element("testsuite", tests="2", failures="0", errors="0", skipped="1")
    ET.SubElement(suite, "testcase", classname="tests.test_app", name="passes")
    case = ET.SubElement(suite, "testcase", classname=classname, name=name)
    ET.SubElement(case, "skipped", message=message)
    ET.ElementTree(suite).write(directory / "junit.xml")
    monkeypatch.setenv("VERIFICATION_DEPENDENCY_PROFILE", profile)
    # When classifying the lane, then only its declared profile permits this skip.
    assert collect(directory, ["pytest"], 0).status == (
        "PASS" if profile == "minimum" else "INCOMPLETE"
    )


def test_minimum_profile_rejects_known_reason_on_unrelated_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a minimum lane with a misleading skip reason on an unrelated test.
    directory = tmp_path / "test"
    directory.mkdir()
    (directory / "junit.xml").write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1">'
        '<testcase/><testcase classname="tests.test_app" name="unrelated">'
        "<skipped message=\"langgraph&lt;1.1 does not support version='v2'\"/>"
        "</testcase></testsuite>"
    )
    monkeypatch.setenv("VERIFICATION_DEPENDENCY_PROFILE", "minimum")
    # When collecting evidence, then the reason alone never authorizes a skip.
    assert collect(directory, ["pytest"], 0).status == "INCOMPLETE"
