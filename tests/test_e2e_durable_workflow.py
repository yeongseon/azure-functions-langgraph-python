"""Pin the optional lane's own-app restart controls and strict no-skip evidence gate."""

from pathlib import Path
import shlex

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_durable_controls_target_only_the_run_app_and_validator_stays_strict() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/e2e-azure-durable.yml").read_text())
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    job = workflow["jobs"]["deploy_and_test"]
    assert job["environment"] == "azure-e2e"
    assert workflow["permissions"] == {"id-token": "write", "contents": "read"}
    [step] = [step for step in job["steps"] if step.get("name") == "Run Durable e2e tests"]
    target = "--resource-group ${{ steps.names.outputs.rg }} --name ${{ steps.names.outputs.app }}"
    for operation in ("restart", "stop", "start"):
        assert (
            step["env"][f"E2E_DURABLE_{operation.upper()}_CMD"]
            == f"az functionapp {operation} {target}"
        )
    assert (
        step["env"]["E2E_DURABLE_STATE_CMD"] == f"az functionapp show {target} --query state -o tsv"
    )
    lines = step["run"].replace("\\\n", " ").splitlines()
    [pytest_line] = [line.strip() for line in lines if line.strip().startswith("pytest ")]
    [validator] = [
        line.strip() for line in lines if line.strip().startswith("python tools/check_junit.py ")
    ]
    words = shlex.split(pytest_line)
    assert "tests/e2e_durable" in words and "--tb=short" in words
    assert "-x" in words
    assert "--junitxml=cert-durable/e2e-results.xml" in words
    assert "cert-durable/e2e-results.xml" in shlex.split(validator)
    assert "test_durable_e2e" in validator
    assert lines.index(next(line for line in lines if line.strip() == pytest_line)) < lines.index(
        next(line for line in lines if line.strip() == validator)
    )
    assert not {"if", "continue-on-error"} & step.keys()
    assert "||" not in step["run"] and "set +e" not in step["run"]
