"""Offline security contract for the pre-tag real-Azure release canary."""

from calendar import day_name
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/azure-canary.yml"
CALLEE = WORKFLOW.with_name("e2e-azure.yml")
RELEASE_DOCS = WORKFLOW.parents[2] / "docs/release_process.md"


class CanaryJob(TypedDict):
    permissions: dict[str, str]
    uses: str
    secrets: dict[str, str]


class CanaryWorkflow(TypedDict):
    name: str
    on: dict[str, list[dict[str, str]] | None]
    permissions: dict[str, str]
    concurrency: dict[str, str | bool]
    jobs: dict[str, CanaryJob]


CertificationJob = TypedDict("CertificationJob", {"environment": str, "if": str}, total=False)


class CertificationTrigger(TypedDict, total=False):
    inputs: dict[str, dict[str, str]]
    secrets: dict[str, dict[str, str]]


class CertificationWorkflow(TypedDict):
    on: dict[str, CertificationTrigger]
    permissions: dict[str, str]
    jobs: dict[str, CertificationJob]


@pytest.fixture
def workflow() -> CanaryWorkflow:
    # Given the committed YAML, when parsed (quoted "on" avoids YAML 1.1 booleans).
    document: CanaryWorkflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return document


@pytest.fixture
def certification() -> CertificationWorkflow:
    # BaseLoader preserves the callee's unquoted "on" key instead of YAML 1.1's True.
    document: CertificationWorkflow = yaml.load(
        CALLEE.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    return document


def test_canary_has_only_weekly_schedule_and_input_free_dispatch(workflow: CanaryWorkflow) -> None:
    assert workflow["name"] == "azure-canary"
    assert workflow["on"] == {
        "schedule": [{"cron": "41 2 * * 1"}],
        "workflow_dispatch": None,
    }


def test_canary_defaults_to_read_only_contents(workflow: CanaryWorkflow) -> None:
    assert workflow["permissions"] == {"contents": "read"}


def test_canary_serializes_runs_without_cancelling_cleanup(workflow: CanaryWorkflow) -> None:
    assert workflow["concurrency"] == {"group": "azure-canary", "cancel-in-progress": False}


def test_canary_only_calls_existing_certification_without_inputs(workflow: CanaryWorkflow) -> None:
    [job] = workflow["jobs"].values()
    # No with/ref/version/certify_2x override, extra steps, or unsupported caller keys.
    assert set(job) == {"uses", "permissions", "secrets"}
    assert job["uses"] == "./.github/workflows/e2e-azure.yml"


def test_canary_grants_oidc_only_to_certification(workflow: CanaryWorkflow) -> None:
    [job] = workflow["jobs"].values()
    assert job["permissions"] == {"id-token": "write", "contents": "read"}


def test_canary_maps_only_the_three_azure_secrets(workflow: CanaryWorkflow) -> None:
    [job] = workflow["jobs"].values()
    # Exact mapping rejects inherit, extra credentials, and swapped secret values.
    assert job["secrets"] == {
        "AZURE_CLIENT_ID": "${{ secrets.AZURE_CLIENT_ID }}",
        "AZURE_TENANT_ID": "${{ secrets.AZURE_TENANT_ID }}",
        "AZURE_SUBSCRIPTION_ID": "${{ secrets.AZURE_SUBSCRIPTION_ID }}",
    }


def test_certification_pins_every_jobs_oidc_environment(
    certification: CertificationWorkflow,
) -> None:
    for name, job in certification["jobs"].items():
        assert job.get("environment") == "azure-e2e", name


def test_certification_requests_only_oidc_and_read_only_contents(
    certification: CertificationWorkflow,
) -> None:
    assert certification["permissions"] == {"id-token": "write", "contents": "read"}


def test_certification_attempts_cleanup_after_failure(certification: CertificationWorkflow) -> None:
    assert certification["jobs"]["cleanup"].get("if") == "always()"


def test_certification_has_only_dispatch_and_reusable_triggers(
    certification: CertificationWorkflow,
) -> None:
    assert set(certification["on"]) == {"workflow_dispatch", "workflow_call"}


def test_canary_documented_schedule_matches_workflow(workflow: CanaryWorkflow) -> None:
    section = RELEASE_DOCS.read_text(encoding="utf-8").split("### Real-Azure canary\n", 1)[1]
    section = section.split("\n### ", 1)[0]
    schedule = workflow["on"]["schedule"]
    assert schedule is not None
    [entry] = schedule
    minute, hour, _, _, weekday = entry["cron"].split()
    assert f"{day_name[int(weekday) - 1]} at {int(hour):02}:{int(minute):02} UTC" in section
    assert "azure-canary.yml" in section
