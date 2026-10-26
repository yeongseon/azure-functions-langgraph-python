from pathlib import Path


def test_dispatch_token_cannot_follow_redirects_or_use_plain_http() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/publish-pypi.yml").read_text()
    dispatch = workflow.split("  notify-cookbook:", 1)[1]
    assert '--proto "=https"' in dispatch
    assert "--max-redirs 0" in dispatch
    assert "--connect-timeout 10" in dispatch
    assert "--max-time 30" in dispatch
    assert "curl -v" not in dispatch
    assert "set -x" not in dispatch
