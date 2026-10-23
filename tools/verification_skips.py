from __future__ import annotations

import os
import xml.etree.ElementTree as ET


def expected_skip(case: ET.Element, lane: str) -> bool:
    skip = case.find("skipped")
    if skip is None or lane not in {"test", "dependency", "wheel-suite"}:
        return False
    identity = (case.get("classname", ""), case.get("name", ""))
    message = skip.get("message", "")
    if identity in {
        ("tests.test_release_workflow_pins", "test_detects_missing_runtime_tier"),
        ("test_release_workflow_pins", "test_detects_missing_runtime_tier"),
    }:
        return message == "minimal family has no required runtime tier"
    if lane == "wheel-suite" or os.environ.get("VERIFICATION_DEPENDENCY_PROFILE") != "minimum":
        return False
    match identity:
        case (
            "tests.test_integration.TestNativeVersionPassthrough",
            "test_invoke_v2_returns_graph_output_envelope"
            | "test_stream_v2_emits_streampart_frames",
        ):
            return message == "langgraph<1.1 does not support version='v2'"
        case ("", "tests.test_openapi_real_companion_interop"):
            return message == "collection skipped" and (
                "Skipped: requires the optional 'openapi' extra: "
                "pip install azure-functions-langgraph[openapi]"
            ) in (skip.text or "")
        case ("tests.test_checkpointers_azure_blob", "test_conformance_base_capabilities"):
            return message == "langgraph-checkpoint-conformance not installed"
        case (
            "tests.test_sqlite_walkthrough",
            "test_sqlite_walkthrough_matches_live_handler_output",
        ):
            return message == (
                "SQLite extra is unavailable or incompatible: SQLite checkpointer requires "
                "the 'sqlite' extra: pip install azure-functions-langgraph[sqlite]"
            )
        case (
            "tests.test_streaming_app.TestAiterGraphEvents",
            "test_sync_mode_uses_stream_with_sqlite_saver",
        ):
            return message.startswith(
                "installed SQLite saver is incompatible: cannot import name 'DeltaChannelHistory' "
                "from 'langgraph.checkpoint.base' ("
            ) and message.endswith("/langgraph/checkpoint/base/__init__.py)")
        case _:
            return False
