from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci-test.yml"
CLASSIFIER = ROOT / "tools" / "ci_classify_changes.sh"
FULL_MATRIX = "docs_only=false\ndocs_changed=true\nfull_required=true\n"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    process: subprocess.CompletedProcess[str]
    output: str


def _classification_program() -> str:
    workflow = CI_WORKFLOW.read_text()
    match = re.search(
        r"- name: Classify changed files.*?\n\s+run: \|\n(?P<body>.*?)(?=\n  [a-z][a-z0-9-]+:)",
        workflow,
        re.DOTALL,
    )
    assert match is not None
    lines = match.group("body").splitlines()
    indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indent:] for line in lines)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / CLASSIFIER.name).write_text(CLASSIFIER.read_text())
    (repo / "README.md").write_text("base\n")
    _git(repo, "init")
    _git(repo, "config", "user.name", "CI Test")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("head\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _run_classification(
    repo: Path,
    tmp_path: Path,
    *,
    event_name: str,
    base: str,
    head: str,
    before: str,
    git_wrapper: str | None = None,
    head_repository: str = "owner/repo",
    fork_head: str = "",
) -> ClassificationResult:
    output = tmp_path / "github-output"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if git_wrapper is not None:
        wrapper = bin_dir / "git"
        wrapper.write_text(git_wrapper)
        wrapper.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REAL_GIT": subprocess.run(
            ["which", "git"], capture_output=True, text=True, check=True
        ).stdout.strip(),
        "GITHUB_OUTPUT": str(output),
        "EVENT_NAME": event_name,
        "BASE_SHA": base,
        "HEAD_SHA": head,
        "BEFORE_SHA": before,
        "SHA": head,
        "HEAD_REPOSITORY": head_repository,
        "REPOSITORY": "owner/repo",
        "PR_NUMBER": "1",
        "FORK_HEAD": fork_head,
    }
    result = subprocess.run(
        ["bash", "-c", _classification_program()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return ClassificationResult(process=result, output=output.read_text())


@pytest.mark.parametrize("event_name", ["pull_request", "push"])
def test_failing_git_diff_fails_safe_for_every_event(tmp_path: Path, event_name: str) -> None:
    repo, base, head = _repo(tmp_path)
    wrapper = """#!/usr/bin/env bash
if [ "$1" = diff ]; then exit 2; fi
exec "$REAL_GIT" "$@"
"""

    result = _run_classification(
        repo,
        tmp_path,
        event_name=event_name,
        base=base,
        head=head,
        before=base,
        git_wrapper=wrapper,
    )

    assert result.process.returncode == 0
    assert result.output == FULL_MATRIX


def test_fork_head_classifier_is_data_not_executable(tmp_path: Path) -> None:
    repo, base, _ = _repo(tmp_path)
    _git(repo, "checkout", "-b", "fork")
    (repo / "tools" / CLASSIFIER.name).write_text(
        "#!/usr/bin/env bash\n"
        "printf 'docs_only=true\\ndocs_changed=true\\nfull_required=false\\n'\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "malicious classifier")
    fork_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--detach", base)
    wrapper = """#!/usr/bin/env bash
if [ "$1" = fetch ]; then
  exec "$REAL_GIT" update-ref refs/remotes/origin/pr-head "$FORK_HEAD"
fi
exec "$REAL_GIT" "$@"
"""
    result = _run_classification(
        repo,
        tmp_path,
        event_name="pull_request",
        base=base,
        head=fork_head,
        before=base,
        git_wrapper=wrapper,
        head_repository="fork/repo",
        fork_head=fork_head,
    )

    assert result.process.returncode == 0, result.process.stderr
    assert result.output == FULL_MATRIX


def test_non_ancestor_push_range_fails_safe(tmp_path: Path) -> None:
    repo, base, _ = _repo(tmp_path)
    _git(repo, "checkout", "--detach", base)
    (repo / "README.md").write_text("replacement\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "replacement head")
    replacement_head = _git(repo, "rev-parse", "HEAD")

    result = _run_classification(
        repo,
        tmp_path,
        event_name="push",
        base=base,
        head=replacement_head,
        before=_git(repo, "rev-parse", "master"),
    )

    assert result.process.returncode == 0
    assert result.output == FULL_MATRIX
