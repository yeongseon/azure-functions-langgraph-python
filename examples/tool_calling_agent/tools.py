"""Deterministic demo tools for the tool_calling_agent example.

These tools are **your LangGraph application's** business logic — they are not
provided by ``azure-functions-langgraph``. The package only exposes the compiled
graph over HTTP; whatever the tools do is up to you.

Each tool here is intentionally deterministic, local, and dependency-free so the
example runs (and CI tests) without any external SaaS API. **This is where you
would call a REST API, an SDK, a database, or another Azure Function instead.**
See the README's "Replace the demo tools" section.
"""

from __future__ import annotations

from langchain_core.tools import tool

# In-memory demo "database". Swap this for a real datastore / API call.
_ORDERS: dict[str, dict[str, str]] = {
    "A1001": {"status": "shipped", "carrier": "Contoso Express", "eta": "2 days"},
    "A1002": {"status": "processing", "carrier": "-", "eta": "5 days"},
}

_WEATHER: dict[str, str] = {
    "seattle": "12C, light rain",
    "seoul": "21C, sunny",
    "tokyo": "24C, cloudy",
}


@tool
def lookup_order(order_id: str) -> str:
    """Look up the delivery status of a customer order by its ID.

    Args:
        order_id: The order identifier, e.g. ``"A1001"``.
    """
    order = _ORDERS.get(order_id.strip().upper())
    if order is None:
        return f"No order found with id {order_id!r}."
    return (
        f"Order {order_id.strip().upper()}: {order['status']} via "
        f"{order['carrier']}, ETA {order['eta']}."
    )


@tool
def get_weather(city: str) -> str:
    """Return the current demo weather for a city.

    Args:
        city: The city name, e.g. ``"Seoul"``.
    """
    report = _WEATHER.get(city.strip().lower())
    if report is None:
        return f"No demo weather available for {city!r}."
    return f"Weather in {city.strip().title()}: {report}."


# The list the agent binds and the ToolNode executes. Add your own tools here.
TOOLS = [lookup_order, get_weather]
