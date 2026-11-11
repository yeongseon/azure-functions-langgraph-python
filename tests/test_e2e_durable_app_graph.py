"""Offline check of the evidence graph in ``examples/e2e_durable_app`` (#811).

The real-Azure lane (``tests/e2e_durable``) cannot run in CI, so this verifies locally, against an
in-memory blob container, the parts it relies on: one attempt marker per node run, and an
idempotency key that applies the effect once however often the tool is called.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from azure.core.exceptions import ResourceExistsError
import pytest

from tests.example_imports import load_example_file

GRAPH = Path(__file__).resolve().parents[1] / "examples" / "e2e_durable_app" / "graph.py"


class _Blob:
    def __init__(self, name: str) -> None:
        self.name = name


class _Download:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def upload_blob(self, name: str, data: bytes, overwrite: bool = True) -> None:
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("exists")
        self.blobs[name] = data

    def list_blobs(self, name_starts_with: str = "") -> list[_Blob]:
        return [_Blob(n) for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def download_blob(self, name: str) -> _Download:
        return _Download(self.blobs[name])


@pytest.fixture
def graph_module(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, FakeContainer]:
    module = load_example_file(GRAPH, "e2e_durable_graph")
    fake = FakeContainer()
    monkeypatch.setattr(module, "evidence_container", lambda: fake)
    return module, fake


def test_effect_is_applied_once_per_idempotency_key(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    module, _ = graph_module
    state: Any = {"run_id": "r1"}
    first = module.effect(state)
    second = module.effect(state)  # a redelivered Activity calls the tool again
    assert first == {"effect_applied": True}
    assert second == {"effect_applied": False}
    assert len(module.count_markers("effect-call", "r1")) == 2
    assert module.effect_applied_count("r1") == 1


def test_attempt_and_completion_markers_are_counted_per_run(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    module, _ = graph_module
    module.enter({"run_id": "r2"})
    module.enter({"run_id": "r2"})
    module.hold({"run_id": "r2", "hold_seconds": 0})
    module.enter({"run_id": "other"})
    assert len(module.count_markers("attempt", "r2")) == 2
    assert len(module.count_markers("completed", "r2")) == 1
    assert all(len(tag) == 8 for tag in module.count_markers("attempt", "r2"))


def test_fail_flag_raises_before_any_effect(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    module, _ = graph_module
    with pytest.raises(RuntimeError):
        module.effect({"run_id": "r3", "fail": True})
    assert module.effect_applied_count("r3") == 0
