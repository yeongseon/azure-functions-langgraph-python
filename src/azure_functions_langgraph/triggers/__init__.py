"""Event-driven trigger adapters for LangGraph graphs.

Currently ships the Azure Service Bus trigger adapter (issue #409). Trigger
adapters let a compiled graph be driven by a broker message rather than an HTTP
request, while reusing the package's thread-lock and observability contracts.
"""

from __future__ import annotations

from azure_functions_langgraph.triggers.service_bus import (
    InputMapper,
    ResultHandler,
    ServiceBusMessageLike,
    ThreadContentionError,
    ThreadIdFactory,
    default_message_mapper,
    process_service_bus_message,
    process_service_bus_message_async,
)

__all__ = [
    "InputMapper",
    "ResultHandler",
    "ServiceBusMessageLike",
    "ThreadContentionError",
    "ThreadIdFactory",
    "default_message_mapper",
    "process_service_bus_message",
    "process_service_bus_message_async",
]
