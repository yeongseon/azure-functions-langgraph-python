"""Admission for untrusted Durable HTTP create requests."""

from collections.abc import Mapping
from typing import Any

from azure_functions_langgraph._validation import validate_input_structure, validate_thread_id


def validate_run_request(body: Mapping[str, Any], max_depth: int, max_nodes: int) -> str | None:
    """Return a value-free error before constructing a Durable payload."""
    config = body.get("config")
    if config is not None and not isinstance(config, Mapping):
        return "config must be a JSON object or null"
    configurable = config.get("configurable") if config is not None else None
    if configurable is not None and not isinstance(configurable, Mapping):
        return "config.configurable must be a JSON object or null"
    thread_id = body.get("thread_id")
    if thread_id is None and configurable is not None:
        thread_id = configurable.get("thread_id")
    if thread_id is not None:
        if not isinstance(thread_id, str):
            return "thread_id must be a string or null"
        error = validate_thread_id(thread_id)
        if error is not None:
            return error
        if not thread_id.isascii() or not thread_id.isprintable():
            return "Thread ID must contain only printable ASCII characters"
    for value in (body.get("input"), config):
        error = validate_input_structure(value, max_depth=max_depth, max_nodes=max_nodes)
        if error is not None:
            return error
    return None
