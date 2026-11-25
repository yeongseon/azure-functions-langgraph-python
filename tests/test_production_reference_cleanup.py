"""Run the copy-paste deployment block offline, with no real Azure CLI on PATH."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final

import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
FAKE_AZ: Final = r"""#!/bin/bash
set -eu
printf '%s\t' "$@" >> "$SANDBOX/calls"
printf '\n' >> "$SANDBOX/calls"
command="$1 $2"
shift 2
query= name= subscription=
while (( $# )); do
  case "$1" in
    --query) query=$2; shift 2 ;;
    --name|--resource-group) name=$2; shift 2 ;;
    --subscription) subscription=$2; shift 2 ;;
    execution=*) printf '%s' "${1#execution=}" > "$SANDBOX/tag"; shift ;;
    *) shift ;;
  esac
done
case "$command" in
  'account show') printf '%s\n' subscription-owned ;;
  'account set') ;;
  'bicep build') ;;
  'group exists')
    if [[ -f "$SANDBOX/group" ]]; then echo true; else echo false; fi ;;
  'group create')
    if [[ "$SCENARIO" == create-failure ]]; then echo 'fake create failed' >&2; exit 41; fi
    printf '%s' "$name" > "$SANDBOX/group"
    printf '%s' "$subscription" > "$SANDBOX/subscription"
    if [[ "$SCENARIO" == initial-tag-mismatch ]]; then echo other > "$SANDBOX/tag"; fi ;;
  'group show')
    if [[ -f "$SANDBOX/deployed" && "$SCENARIO" == show-failure ]]; then
      echo 'fake ownership read failed' >&2; exit 73
    fi
    if [[ "$query" == id ]]; then
      if [[ "$SCENARIO" == subscription-mismatch && -f "$SANDBOX/deployed" ]]; then
        subscription=subscription-other
      fi
      printf '/subscriptions/%s/resourceGroups/%s\n' "$subscription" "$name"
    elif [[ "$query" == tags.execution ]]; then
      while IFS= read -r -n 1 char; do printf '%s' "$char"; done < "$SANDBOX/tag"
      printf '\n'
    else
      echo 'unsupported fake query' >&2; exit 90
    fi ;;
  'deployment group')
    : > "$SANDBOX/deployed"
    if [[ "$SCENARIO" == tag-mismatch ]]; then echo other > "$SANDBOX/tag"; fi
    echo 'fake deployment failed' >&2; exit 42 ;;
  'group delete')
    if [[ "$SCENARIO" == delete-failure ]]; then echo 'fake delete failed' >&2; exit 71; fi
    rm "$SANDBOX/group" ;;
  'group wait')
    if [[ "$SCENARIO" == wait-failure ]]; then echo 'fake wait failed' >&2; exit 72; fi ;;
  *) echo "unsupported fake command: $command" >&2; exit 91 ;;
esac
"""


@dataclass(frozen=True, slots=True)
class Run:
    process: subprocess.CompletedProcess[str]
    calls: tuple[tuple[str, ...], ...]
    group_exists: bool


def run_deployment(tmp_path: Path, scenario: str, page: str | None = None) -> Run:
    """Use a fresh, allow-listed PATH; unexpected tools cannot reach cloud credentials."""
    sandbox = tmp_path / "bin"
    sandbox.mkdir()
    for tool in ("bash", "date", "mkdir", "rm"):
        executable = shutil.which(tool)
        assert executable is not None
        (sandbox / tool).symlink_to(executable)
    (sandbox / "python3").symlink_to(sys.executable)
    for name, script in {
        "az": FAKE_AZ,
        "git": "#!/bin/bash\nmkdir azure-functions-langgraph-python\n",
        "make": "#!/bin/bash\nexit 0\n",
    }.items():
        target = sandbox / name
        target.write_text(script)
        target.chmod(0o700)
    if scenario == "pre-existing":
        (tmp_path / "group").write_text("existing-group")
        (tmp_path / "tag").write_text("someone-else")
    source = page if page is not None else (ROOT / "docs/production-reference.md").read_text()
    block = source.split("## Deploy from a clean checkout", 1)[1].split("```bash\n", 1)[1]
    commands = block.split("```", 1)[0]
    process = subprocess.run(
        [str(sandbox / "bash"), "--noprofile", "--norc", "-c", commands],
        cwd=tmp_path,
        env={
            "PATH": str(sandbox),
            "SANDBOX": str(tmp_path),
            "SCENARIO": scenario,
            "SUBSCRIPTION": "chosen-subscription-name",
        },
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    calls = tuple(
        tuple(line.rstrip("\t").split("\t"))
        for line in (tmp_path / "calls").read_text().splitlines()
    )
    return Run(process, calls, (tmp_path / "group").exists())


def test_existing_group_is_untouched(tmp_path: Path) -> None:
    # Given an existing group at the intended name, even if a collision occurs.
    # When the actual documented block runs.
    run = run_deployment(tmp_path, "pre-existing")
    # Then neither create/update nor delete is attempted.
    assert run.process.returncode != 0
    assert run.group_exists, run.calls
    assert not any(call[:2] in (("group", "create"), ("group", "delete")) for call in run.calls)


def test_group_names_are_execution_specific(tmp_path: Path) -> None:
    # Given two independent executions with the same subscription.
    runs = []
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        runs.append(directory)
    # When each executes the documented block.
    results = [run_deployment(directory, "deployment-failure") for directory in runs]
    # Then each group has its own execution tag and name.
    names = []
    for run in results:
        create = next(call for call in run.calls if call[:2] == ("group", "create"))
        name = create[create.index("--name") + 1]
        execution = next(
            value.removeprefix("execution=") for value in create if value.startswith("execution=")
        )
        assert name == f"rg-af-ref-{execution}"
        names.append(name)
    assert names[0] != names[1]


@pytest.mark.parametrize("scenario", ["create-failure", "initial-tag-mismatch"])
def test_cleanup_stays_unarmed_until_ownership_is_established(
    tmp_path: Path, scenario: str
) -> None:
    # Given unsuccessful creation or a newly created group that fails ownership verification.
    # When deployment is attempted.
    run = run_deployment(tmp_path, scenario)
    # Then cleanup never deletes and provisioning never starts.
    assert run.process.returncode != 0
    assert not any(call[:2] == ("group", "delete") for call in run.calls)
    assert not any(call[0] == "deployment" for call in run.calls)


def test_owned_group_is_deleted_after_deployment_failure(tmp_path: Path) -> None:
    # Given successful group creation and a later provisioning error.
    # When the shell exits.
    run = run_deployment(tmp_path, "deployment-failure")
    # Then the original failure survives and only the created subscription/group is deleted.
    assert run.process.returncode == 42, run.process.stderr
    assert not run.group_exists
    create = next(call for call in run.calls if call[:2] == ("group", "create"))
    delete = next(call for call in run.calls if call[:2] == ("group", "delete"))
    assert create[create.index("--name") + 1] == delete[delete.index("--name") + 1]
    for call in run.calls:
        if call[0] in ("group", "deployment"):
            assert call[call.index("--subscription") + 1] == "subscription-owned"
    assert sum(call[:2] == ("group", "show") for call in run.calls) >= 4


@pytest.mark.parametrize("scenario", ["tag-mismatch", "subscription-mismatch", "show-failure"])
def test_cleanup_refuses_unverified_group(tmp_path: Path, scenario: str) -> None:
    # Given changed ownership or an unreadable ownership record after provisioning starts.
    # When EXIT cleanup rechecks the group.
    run = run_deployment(tmp_path, scenario)
    # Then the group survives, with a visible cleanup failure.
    assert run.group_exists, run.calls
    assert not any(call[:2] == ("group", "delete") for call in run.calls)
    assert run.process.returncode == 1
    assert "cleanup" in run.process.stderr.lower()


@pytest.mark.parametrize("scenario", ["delete-failure", "wait-failure"])
def test_cleanup_errors_are_visible(tmp_path: Path, scenario: str) -> None:
    # Given an owned group whose deletion or deletion wait fails.
    # When EXIT cleanup runs.
    run = run_deployment(tmp_path, scenario)
    # Then cleanup's failure is visible alongside the deployment failure.
    assert run.process.returncode == 1
    assert f"fake {scenario.replace('-failure', '')} failed" in run.process.stderr
    assert "cleanup" in run.process.stderr.lower()
