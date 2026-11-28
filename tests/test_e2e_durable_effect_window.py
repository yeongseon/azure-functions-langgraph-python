"""Offline behavior of the opt-in post-effect interruption window."""

from types import ModuleType

import pytest

from tests.test_e2e_durable_app_graph import FakeContainer

pytest_plugins = ["tests.test_e2e_durable_app_graph"]


def test_effect_hold_is_skipped_when_key_already_applied(
    graph_module: tuple[ModuleType, FakeContainer], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a committed effect whose node has not returned yet.
    module, fake = graph_module
    monkeypatch.setenv("E2E_POST_EFFECT_HOLD_SECONDS", "240")
    holds: list[float] = []

    def hold(seconds: float) -> None:
        assert fake.blobs["effect-applied/window"] == b"1"
        holds.append(seconds)

    monkeypatch.setattr(module.time, "sleep", hold)
    module.effect({"run_id": "window"})

    # When redelivery invokes the same effect key again.
    result = module.effect({"run_id": "window"})

    # Then the duplicate effect and its hold are suppressed.
    assert result == {"effect_applied": False}
    assert holds == [240.0]
    assert module.effect_applied_count("window") == 1
