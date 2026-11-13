"""Offline check of the evidence graph in ``examples/e2e_durable_app`` (#811).

The real-Azure lane (``tests/e2e_durable``) cannot run in CI, so this verifies locally, against an
in-memory blob container, the parts it relies on: Activity-boundary
attempt evidence (kept separate from graph-node entries, including lock rejections), completion
markers emitted only after graph invocation returns, and an idempotency key that applies the
effect once however often the tool is called.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
from types import ModuleType
from typing import Any

import pytest

from tests.example_imports import load_example_file

GRAPH = Path(__file__).resolve().parents[1] / "examples" / "e2e_durable_app" / "graph.py"


class ResourceExistsError(Exception):
    """Stand-in for azure.core.exceptions.ResourceExistsError (azure-core is not a core dep)."""


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
        self.order: list[str] = []  # upload order, for marker-ordering assertions

    def upload_blob(self, name: str, data: bytes, overwrite: bool = True) -> None:
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("exists")
        self.blobs[name] = data
        self.order.append(name)

    def list_blobs(self, name_starts_with: str = "") -> list[_Blob]:
        return [_Blob(n) for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def download_blob(self, name: str) -> _Download:
        return _Download(self.blobs[name])


@pytest.fixture
def graph_module(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, FakeContainer]:
    # The example imports azure.core / azure.storage.blob at module level, which the core CI
    # lanes do not install. Stub them for the import only; monkeypatch restores sys.modules.
    core = types.ModuleType("azure.core")
    exceptions = types.ModuleType("azure.core.exceptions")
    exceptions.ResourceExistsError = ResourceExistsError  # type: ignore[attr-defined]
    storage = types.ModuleType("azure.storage")
    blob = types.ModuleType("azure.storage.blob")
    blob.ContainerClient = object  # type: ignore[attr-defined]
    for name, mod in {
        "azure.core": core,
        "azure.core.exceptions": exceptions,
        "azure.storage": storage,
        "azure.storage.blob": blob,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
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


def test_node_markers_are_counted_per_run_and_separate_from_attempts(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    module, _ = graph_module
    module.enter({"run_id": "r2"})
    module.enter({"run_id": "r2"})
    module.hold({"run_id": "r2", "hold_seconds": 0})
    module.enter({"run_id": "other"})
    assert len(module.count_markers("node-enter", "r2")) == 2
    assert len(module.count_markers("hold-finished", "r2")) == 1
    # Graph nodes never write Activity-boundary evidence.
    assert module.count_markers("attempt", "r2") == []
    assert module.count_markers("activity-finished", "r2") == []
    assert all(len(tag) == 8 for tag in module.count_markers("node-enter", "r2"))


class _Reg:
    def __init__(self, graph: Any) -> None:
        self.graph = graph
        self.name = "e2e_durable"
        self.async_mode = False


def _checkpointed_graph(module: ModuleType) -> Any:
    from langgraph.checkpoint.memory import MemorySaver

    return module.builder.compile(checkpointer=MemorySaver())


async def _run(module: ModuleType, lock: Any, run_id: str, **input_extra: Any) -> Any:
    from azure_functions_langgraph.durable._activity import execute_langgraph_run_impl
    from azure_functions_langgraph.durable._state import DurableRunPayload

    payload = DurableRunPayload(
        run_id=run_id,
        graph_name="e2e_durable",
        input={"run_id": run_id, "hold_seconds": 0, **input_extra},
        thread_id=f"t-{run_id}",
        config={},
    )
    return await execute_langgraph_run_impl(
        payload,
        registry={"e2e_durable": _Reg(_checkpointed_graph(module))},
        thread_lock=lock,
        observer=module.EvidenceObserver(),
    )


async def test_rejected_delivery_counts_as_attempt_but_not_node_entry(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    from azure_functions_langgraph.locks import InProcessThreadLock

    module, _ = graph_module
    lock = InProcessThreadLock()
    assert lock.acquire("e2e_durable", "t-r4")  # an older delivery still holds the lease
    result = await _run(module, lock, "r4")
    assert result.activity_status == "rejected"
    assert len(module.count_markers("attempt", "r4")) == 1
    assert len(module.count_markers("rejected", "r4")) == 1
    assert module.count_markers("started", "r4") == []
    assert module.count_markers("node-enter", "r4") == []
    assert module.count_markers("activity-finished", "r4") == []


async def test_attempts_and_node_entries_diverge_on_redelivery(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    from azure_functions_langgraph.locks import InProcessThreadLock

    module, _ = graph_module
    lock = InProcessThreadLock()
    token = lock.acquire("e2e_durable", "t-r5")
    assert token
    await _run(module, lock, "r5")  # rejected redelivery while the old lease is valid
    lock.release("e2e_durable", "t-r5", token)
    result = await _run(module, lock, "r5")  # the redelivery that finally runs
    assert result.activity_status == "success"
    assert len(module.count_markers("attempt", "r5")) == 2
    assert len(module.count_markers("node-enter", "r5")) == 1
    assert len(module.marker_times_ms("attempt", "r5")) == 2


async def test_activity_finished_is_emitted_after_all_graph_work(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    from azure_functions_langgraph.locks import InProcessThreadLock

    module, fake = graph_module
    result = await _run(module, InProcessThreadLock(), "r6")
    assert result.activity_status == "success"
    kinds = [n.split("/", 1)[0] for n in fake.order]
    assert kinds[0] == "attempt"
    assert kinds.index("hold-finished") < kinds.index("effect-call")
    assert kinds.index("effect-call") < kinds.index("effect-applied")
    # the completion signal is the very last evidence written, after the effect node finished
    assert kinds[-1] == "activity-finished" and kinds.count("activity-finished") == 1
    assert kinds.index("hold-finished") < kinds.index("activity-finished")
    (finished_ms,) = module.marker_times_ms("activity-finished", "r6")
    assert finished_ms >= max(module.marker_times_ms("effect-call", "r6"))


async def test_failed_activity_still_emits_finish_marker_after_effect_node_raised(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    from azure_functions_langgraph.locks import InProcessThreadLock

    module, fake = graph_module
    result = await _run(module, InProcessThreadLock(), "r7", fail=True)
    assert result.activity_status == "error"
    assert [n.split("/", 1)[0] for n in fake.order][-1] == "activity-finished"
    assert module.effect_applied_count("r7") == 0


def test_fail_flag_raises_before_any_effect(
    graph_module: tuple[ModuleType, FakeContainer],
) -> None:
    module, _ = graph_module
    with pytest.raises(RuntimeError):
        module.effect({"run_id": "r3", "fail": True})
    assert module.effect_applied_count("r3") == 0
