"""LangGraph Platform API compatibility layer.

This subpackage provides opt-in compatibility with the LangGraph Platform
SDK (``langgraph-sdk``).  When enabled via ``LangGraphApp(platform_compat=True)``,
additional routes are registered that mirror the LangGraph Platform REST API,
allowing the official SDK client to communicate with Azure Functions–hosted graphs.

Modules:

- ``contracts`` — Pydantic models for Platform API request/response shapes
- ``stores`` — ThreadStore protocol and in-memory implementation
- ``routes`` — Route registration for Platform-compatible endpoints
- ``sse`` — SSE format rewriter (Platform wire format)

.. versionadded:: 0.3.0
"""
