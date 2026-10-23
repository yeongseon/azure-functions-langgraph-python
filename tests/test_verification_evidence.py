from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


def test_evidence_policy_is_available() -> None:
    # Given the canonical verification contract, when locating its shared policy,
    # then local and CI runners have one importable implementation.
    assert importlib.util.find_spec("tools.verification_evidence") is not None


@pytest.mark.parametrize(
    ("outcomes", "code", "status"),
    [
        ((), 0, "INCOMPLETE"),
        (("skipped",), 0, "INCOMPLETE"),
        (("pass", "skipped"), 0, "INCOMPLETE"),
        (("failure",), 1, "FAIL"),
        (("pass",), 0, "PASS"),
        (("pass",), 124, "FAIL"),
    ],
)
def test_required_outcomes_fail_closed(
    tmp_path: Path, outcomes: tuple[str, ...], code: int, status: str
) -> None:
    # Given genuine JUnit counters and cases.
    suite = ET.Element("testsuite", tests=str(len(outcomes)), failures="0", errors="0", skipped="0")
    for outcome in outcomes:
        case = ET.SubElement(suite, "testcase", classname="storage", name="roundtrip")
        if outcome != "pass":
            ET.SubElement(case, outcome)
            counter = "failures" if outcome == "failure" else outcome
            suite.set(counter, str(int(suite.get(counter, "0")) + 1))
    ET.ElementTree(suite).write(tmp_path / "junit.xml")
    # When classifying required execution, then absence/skips never pass.
    policy = importlib.import_module("tools.verification_evidence")
    assert policy.collect(tmp_path, ["pytest"], code).status == status


def test_forged_junit_counters_are_rejected(tmp_path: Path) -> None:
    # Given a report that hides a skipped case, when parsing, then reject it.
    (tmp_path / "junit.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        "<testcase><skipped/></testcase></testsuite>"
    )
    policy = importlib.import_module("tools.verification_evidence")
    with pytest.raises(ValueError, match="counter"):
        policy.collect(tmp_path, ["pytest"], 0)


@pytest.mark.parametrize(
    "secret",
    [
        "AccountKey=CANARYsecret;",
        "SharedAccessSignature=CANARYsecret;",
        "code=CANARYsecret&x=1",
        "x-functions-key: CANARYsecret",
        "Authorization: Bearer CANARYsecret",
        "token=CANARYsecret",
        '"connection_string": "CANARYsecret"',
        "COSMOS_EMULATOR_KEY=CANARYsecret",
        "sig=CANARYsecret",
        "sas=CANARYsecret",
        "password=CANARYsecret",
    ],
)
def test_redaction_covers_all_evidence_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secret: str,
) -> None:
    # Given secrets in diagnostics and command arguments.
    policy = importlib.import_module("tools.verification_evidence")
    (tmp_path / "runner.log").write_text(secret)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "job-summary"))
    summary = policy.Summary(1, "table", ("pytest", secret), 1, 1, 0, 0, "PASS", 0)
    # When saving and publishing, then no surface reveals the canary.
    policy.publish(policy.save(tmp_path, summary))
    assert "CANARYsecret" not in capsys.readouterr().out
    for path in tmp_path.iterdir():
        assert "CANARYsecret" not in path.read_text()
    assert json.loads((tmp_path / "summary.json").read_text())["schema_version"] == 1


def test_missing_expected_storage_area_is_incomplete(tmp_path: Path) -> None:
    # Given a passing subset, when requiring all Table areas, then it is incomplete.
    (tmp_path / "junit.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="table_store_integration"/></testsuite>'
    )
    policy = importlib.import_module("tools.verification_evidence")
    assert (
        policy.collect(tmp_path, ["pytest"], 0, expected=("checkpoint_conformance",)).status
        == "INCOMPLETE"
    )


def test_offline_skip_exception_does_not_allow_arbitrary_skips(tmp_path: Path) -> None:
    directory = tmp_path / "test"
    directory.mkdir()
    (directory / "junit.xml").write_text(
        '<testsuite tests="2" failures="0" errors="0" skipped="1">'
        '<testcase/><testcase name="arbitrary"><skipped message="unavailable"/>'
        "</testcase></testsuite>"
    )
    policy = importlib.import_module("tools.verification_evidence")
    assert policy.collect(directory, ["pytest"], 0).status == "INCOMPLETE"
