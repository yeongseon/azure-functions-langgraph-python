from collections.abc import Mapping


def require_session_exclusive_topology(
    *, session: bool, isolated: bool, binding: Mapping[str, object]
) -> None:
    if not session or not isolated:
        raise ValueError(
            "session-exclusive execution requires session=True and an app with only one "
            "Service Bus registration, no HTTP graphs, Platform routes or Durable runs"
        )
    if binding.get("cardinality", "one") != "one":
        raise ValueError("session-exclusive execution requires single-message cardinality")
