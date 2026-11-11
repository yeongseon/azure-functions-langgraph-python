from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_release_notes_job_preserves_safe_root_output_wiring() -> None:
    # Given: the checked-in release workflow
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text()
    job = workflow.split("  standardize-release-notes:\n", 1)[1]
    # When / Then: root outputs gate the job and reach the shell only through env
    assert "release_created: ${{ steps.release.outputs.release_created }}" in workflow
    assert "tag_name: ${{ steps.release.outputs.tag_name }}" in workflow
    assert "needs: release-please" in job
    assert "if: needs.release-please.outputs.release_created == 'true'" in job
    assert "RELEASE_TAG: ${{ needs.release-please.outputs.tag_name }}" in job
    assert "GH_TOKEN: ${{ github.token }}" in job
    assert "RELEASE_PLEASE_TOKEN" not in job
    permissions = re.search(r"    permissions:\n((?:      [^\n]+\n)+)", job)
    assert permissions is not None
    assert permissions[1] == "      contents: write\n"
    run_lines = re.findall(r"        run: (.*)", job)
    assert not any("${{" in line for line in run_lines)
    assert 'apply "$RELEASE_TAG" --backup "$RUNNER_TEMP/release-notes-backup"' in job


def test_release_notes_job_warns_and_retains_exclusive_backup() -> None:
    # Given: the optional presentation job and the repository's existing artifact pin
    workflow = (ROOT / ".github/workflows/release-please.yml").read_text()
    job = workflow.split("  standardize-release-notes:\n", 1)[1]
    steps = re.split(r"^      - ", job, flags=re.MULTILINE)[1:]
    ci = (ROOT / ".github/workflows/ci-test.yml").read_text()
    pin = re.search(r"actions/upload-artifact@([0-9a-f]{40})", ci)
    assert pin is not None
    # When / Then: only rewrite is tolerated, visibly; all actions remain pinned
    assert "timeout-minutes: 10" in job
    checkout = next(step for step in steps if "actions/checkout@" in step)
    assert "persist-credentials: false" in checkout
    rewrite = next(step for step in steps if "id: rewrite" in step)
    assert "continue-on-error: true" in rewrite
    assert sum("continue-on-error:" in step for step in steps) == 1
    warning = next(step for step in steps if "if: steps.rewrite.outcome == 'failure'" in step)
    assert "::warning::" in warning and '"$GITHUB_STEP_SUMMARY"' in warning
    upload = next(step for step in steps if "actions/upload-artifact@" in step)
    assert f"actions/upload-artifact@{pin[1]}" in upload
    assert "if: always()" in upload and "if-no-files-found: ignore" in upload
    assert "${{ runner.temp }}/release-notes-backup" in upload
    uses = re.findall(r"uses: (\S+)", job)
    assert uses and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)
