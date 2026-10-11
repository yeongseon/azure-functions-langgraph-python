from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_lists_every_public_runtime_limit() -> None:
    # Given: the canonical constructor documentation
    configuration = (ROOT / "docs" / "configuration.md").read_text()

    # When: the documented options are inspected
    documented_limits = {
        name
        for name in (
            "max_stream_response_bytes",
            "max_request_body_bytes",
            "max_input_depth",
            "max_input_nodes",
        )
        if f"### `{name}`" in configuration
    }

    # Then: every public runtime limit has a dedicated entry
    assert documented_limits == {
        "max_stream_response_bytes",
        "max_request_body_bytes",
        "max_input_depth",
        "max_input_nodes",
    }


def test_getting_started_uses_minimal_health_response() -> None:
    # Given: the health example from the getting-started guide
    guide = (ROOT / "docs" / "getting-started.md").read_text()
    health_example = guide.split("**Check health:**", 1)[1].split("## Next steps", 1)[0]

    # When: the example response is inspected
    # Then: it matches the minimal liveness contract and points to details separately
    assert '"status": "ok"' in health_example
    assert '"graphs"' not in health_example
    assert "/api/health/details" in health_example
