"""Offline security contract for the pre-tag real-Azure release canary."""

from pathlib import Path
from typing import TypedDict

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/azure-canary.yml"


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


@pytest.fixture
def workflow() -> CanaryWorkflow:
    # Given the committed YAML, when parsed (quoted "on" avoids YAML 1.1 booleans).
    document: CanaryWorkflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
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
