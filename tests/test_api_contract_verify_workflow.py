"""Contract pins for the optional published-wheel baseline verification lane (#879)."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/api-contract-verify.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_lane_is_scheduled_or_dispatched_never_on_pull_requests() -> None:
    on_block = _text().split("\npermissions:", 1)[0]
    assert "schedule:" in on_block and "workflow_dispatch:" in on_block
    assert "pull_request" not in on_block and "push:" not in on_block


def test_lane_is_read_only_and_secret_free() -> None:
    text = _text()
    assert "permissions:\n  contents: read\n" in text
    assert text.count("permissions:") == 1
    assert "secrets." not in text and "GITHUB_TOKEN" not in text and "github.token" not in text
    assert "persist-credentials: false" in text


def test_actions_are_sha_pinned_consistently_with_existing_workflows() -> None:
    uses = re.findall(r"uses: (\S+) # (\S+)", _text())
    assert uses and len(uses) == len(re.findall(r"uses: ", _text()))
    for ref, _version in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), ref
        name, _, sha = ref.partition("@")
        reference = (ROOT / ".github/workflows/maintenance.yml").read_text(encoding="utf-8")
        assert f"{name}@{sha}" in reference, f"{name} pin differs from maintenance.yml"


def test_lane_runs_verify_baseline_across_a_matrix() -> None:
    text = _text()
    assert "run: python tools/api_contract.py verify-baseline" in text
    assert "fail-fast: false" in text
    assert {"3.11", "3.12", "3.13"} <= set(re.findall(r'python-version: "([\d.]+)"', text))
    assert "langgraph==1.0.0" in text and "langgraph>=1.0,<2.0" in text
    assert not re.search(r"run: .*\$\{\{", text)


def test_lane_is_not_part_of_the_required_gate() -> None:
    ci = (ROOT / ".github/workflows/ci-test.yml").read_text(encoding="utf-8")
    required = ci.split("  ci-required:", 1)[1]
    assert "verify-baseline" not in ci
    assert "api-contract-verify" not in required
