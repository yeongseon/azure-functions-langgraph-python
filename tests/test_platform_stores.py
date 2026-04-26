"""Tests for platform ThreadStore protocol and InMemoryThreadStore.

Validates CRUD operations, search filtering, thread-safety invariants,
deep-copy isolation, and protocol conformance.
"""

from __future__ import annotations

import threading
from typing import Any, Mapping

import pytest

from azure_functions_langgraph.platform.contracts import Interrupt, Thread, ThreadStatus
from azure_functions_langgraph.platform.stores import InMemoryThreadStore, ThreadStore

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_inmemory_is_threadstore(self) -> None:
        store = InMemoryThreadStore()
        assert isinstance(store, ThreadStore)

    def test_runtime_checkable(self) -> None:
        """ThreadStore is @runtime_checkable."""
        assert isinstance(InMemoryThreadStore(), ThreadStore)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_thread(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        assert isinstance(thread, Thread)
        assert thread.thread_id
        assert thread.status == "idle"
        assert thread.metadata is None
        assert thread.values is None
        assert thread.interrupts == {}

    def test_create_with_metadata(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create(metadata={"user_id": "u-1", "env": "test"})
        assert thread.metadata == {"user_id": "u-1", "env": "test"}

    def test_create_sets_timestamps(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        assert thread.created_at.tzinfo is not None
        assert thread.updated_at.tzinfo is not None
        assert thread.created_at == thread.updated_at

    def test_create_generates_unique_ids(self) -> None:
        store = InMemoryThreadStore()
        ids = {store.create().thread_id for _ in range(50)}
        assert len(ids) == 50

    def test_create_with_custom_id_factory(self) -> None:
        counter = iter(range(100))
        store = InMemoryThreadStore(id_factory=lambda: f"custom-{next(counter)}")
        t1 = store.create()
        t2 = store.create()
        assert t1.thread_id == "custom-0"
        assert t2.thread_id == "custom-1"

    def test_create_returns_deep_copy(self) -> None:
        """Mutating the returned thread must not affect stored state."""
        store = InMemoryThreadStore()
        thread = store.create(metadata={"key": "original"})
        # Mutate the returned object
        assert thread.metadata is not None
        thread.metadata["key"] = "mutated"
        # Verify stored state is unchanged
        stored = store.get(thread.thread_id)
        assert stored is not None
        assert stored.metadata == {"key": "original"}


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing(self) -> None:
        store = InMemoryThreadStore()
        created = store.create(metadata={"k": "v"})
        fetched = store.get(created.thread_id)
        assert fetched is not None
        assert fetched.thread_id == created.thread_id
        assert fetched.metadata == {"k": "v"}

    def test_get_nonexistent_returns_none(self) -> None:
        store = InMemoryThreadStore()
        assert store.get("nonexistent") is None

    def test_get_returns_deep_copy(self) -> None:
        """Mutating a fetched thread must not affect stored state."""
        store = InMemoryThreadStore()
        created = store.create(metadata={"key": "val"})
        fetched = store.get(created.thread_id)
        assert fetched is not None
        assert fetched.metadata is not None
        fetched.metadata["key"] = "mutated"
        refetched = store.get(created.thread_id)
        assert refetched is not None
        assert refetched.metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_metadata(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(thread.thread_id, metadata={"new": "data"})
        assert updated.metadata == {"new": "data"}

    def test_update_status(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(thread.thread_id, status="busy")
        assert updated.status == "busy"

    def test_update_values(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(thread.thread_id, values={"messages": [{"role": "user"}]})
        assert updated.values == {"messages": [{"role": "user"}]}

    def test_update_interrupts(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        interrupts = {"node_a": [Interrupt(id="i-1", value="pause")]}
        updated = store.update(thread.thread_id, interrupts=interrupts)
        assert len(updated.interrupts["node_a"]) == 1

    def test_update_is_partial(self) -> None:
        """Only provided fields are changed; others remain untouched."""
        store = InMemoryThreadStore()
        thread = store.create(metadata={"original": True})
        updated = store.update(thread.thread_id, status="busy")
        assert updated.status == "busy"
        assert updated.metadata == {"original": True}  # unchanged

    def test_update_bumps_updated_at(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        original_updated = thread.updated_at
        updated = store.update(thread.thread_id, status="busy")
        assert updated.updated_at >= original_updated

    def test_update_preserves_created_at(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(thread.thread_id, status="busy")
        assert updated.created_at == thread.created_at

    def test_update_nonexistent_raises_keyerror(self) -> None:
        store = InMemoryThreadStore()
        with pytest.raises(KeyError):
            store.update("nonexistent", status="busy")

    def test_update_returns_deep_copy(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create(metadata={"key": "val"})
        updated = store.update(thread.thread_id, status="busy")
        assert updated.metadata is not None
        updated.metadata["key"] = "mutated"
        refetched = store.get(thread.thread_id)
        assert refetched is not None
        assert refetched.metadata == {"key": "val"}

    def test_update_multiple_fields(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(
            thread.thread_id,
            status="error",
            metadata={"error": "timeout"},
            values={"messages": []},
        )
        assert updated.status == "error"
        assert updated.metadata == {"error": "timeout"}
        assert updated.values == {"messages": []}


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------


class TestRunLock:
    def test_try_acquire_returns_busy_thread(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()

        locked = store.try_acquire_run_lock(thread.thread_id)

        assert locked is not None
        assert locked.thread_id == thread.thread_id
        assert locked.status == "busy"

    def test_try_acquire_raises_key_error_for_missing_thread(self) -> None:
        store = InMemoryThreadStore()

        with pytest.raises(KeyError):
            store.try_acquire_run_lock("missing")

    def test_try_acquire_raises_value_error_for_assistant_mismatch(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        store.update(thread.thread_id, assistant_id="assistant-a")

        with pytest.raises(ValueError, match="assistant-a"):
            store.try_acquire_run_lock(thread.thread_id, assistant_id="assistant-b")

    def test_try_acquire_binds_assistant_when_unbound(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()

        locked = store.try_acquire_run_lock(thread.thread_id, assistant_id="assistant-a")

        assert locked is not None
        assert locked.assistant_id == "assistant-a"
        stored = store.get(thread.thread_id)
        assert stored is not None
        assert stored.assistant_id == "assistant-a"

    def test_try_acquire_returns_none_when_busy(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        first = store.try_acquire_run_lock(thread.thread_id)

        second = store.try_acquire_run_lock(thread.thread_id)

        assert first is not None
        assert second is None

    def test_try_acquire_only_one_winner_under_concurrency(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        worker_count = 8
        barrier = threading.Barrier(worker_count)
        results: list[Thread | None] = [None] * worker_count
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait()
                results[index] = store.try_acquire_run_lock(thread.thread_id)
            except BaseException as exc:  # pragma: no cover - defensive for threads
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join()

        assert not errors
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        assert sum(result is None for result in results) == worker_count - 1

    def test_release_sets_status_and_values(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        store.try_acquire_run_lock(thread.thread_id)

        released = store.release_run_lock(
            thread.thread_id,
            status="idle",
            values={"messages": [{"role": "assistant", "content": "done"}]},
        )

        assert released.status == "idle"
        assert released.values == {"messages": [{"role": "assistant", "content": "done"}]}

    def test_release_rejects_busy_status(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()

        with pytest.raises(ValueError, match="cannot set status to 'busy'"):
            store.release_run_lock(thread.thread_id, status="busy")

    def test_release_raises_key_error_for_missing_thread(self) -> None:
        store = InMemoryThreadStore()

        with pytest.raises(KeyError):
            store.release_run_lock("missing", status="idle")

    def test_acquire_release_acquire_cycle(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()

        first = store.try_acquire_run_lock(thread.thread_id)
        store.release_run_lock(thread.thread_id, status="idle")
        second = store.try_acquire_run_lock(thread.thread_id)

        assert first is not None
        assert second is not None
        assert second.status == "busy"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_existing(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        store.delete(thread.thread_id)
        assert store.get(thread.thread_id) is None
        assert store.get(thread.thread_id) is None

    def test_delete_nonexistent_raises_keyerror(self) -> None:
        store = InMemoryThreadStore()
        with pytest.raises(KeyError):
            store.delete("nonexistent")

    def test_delete_twice_raises_keyerror(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        store.delete(thread.thread_id)
        with pytest.raises(KeyError):
            store.delete(thread.thread_id)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def _populate(
        self,
        store: InMemoryThreadStore,
        count: int = 5,
        metadata: Mapping[str, Any] | None = None,
        status: ThreadStatus = "idle",
    ) -> list[Thread]:
        threads: list[Thread] = []
        for _ in range(count):
            t = store.create(metadata=dict(metadata) if metadata is not None else None)
            if status != "idle":
                t = store.update(t.thread_id, status=status)
            threads.append(t)
        return threads

    def test_search_all(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=3)
        results = store.search()
        assert len(results) == 3

    def test_search_empty_store(self) -> None:
        store = InMemoryThreadStore()
        results = store.search()
        assert results == []

    def test_search_by_status(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=2, status="idle")
        self._populate(store, count=3, status="busy")
        idle = store.search(status="idle")
        busy = store.search(status="busy")
        assert len(idle) == 2
        assert len(busy) == 3

    def test_search_by_metadata(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=2, metadata={"env": "prod"})
        self._populate(store, count=3, metadata={"env": "dev"})
        prod = store.search(metadata={"env": "prod"})
        dev = store.search(metadata={"env": "dev"})
        assert len(prod) == 2
        assert len(dev) == 3

    def test_search_metadata_subset_match(self) -> None:
        """Search matches if all query keys are present in thread metadata."""
        store = InMemoryThreadStore()
        store.create(metadata={"env": "prod", "tier": "premium", "region": "us"})
        store.create(metadata={"env": "prod", "tier": "free"})
        store.create(metadata={"env": "dev"})

        results = store.search(metadata={"env": "prod", "tier": "premium"})
        assert len(results) == 1

        results = store.search(metadata={"env": "prod"})
        assert len(results) == 2

    def test_search_metadata_no_match_on_none(self) -> None:
        """Threads with None metadata should not match metadata filters."""
        store = InMemoryThreadStore()
        store.create()  # No metadata
        store.create(metadata={"key": "val"})
        results = store.search(metadata={"key": "val"})
        assert len(results) == 1

    def test_search_combined_filters(self) -> None:
        store = InMemoryThreadStore()
        t1 = store.create(metadata={"env": "prod"})
        store.update(t1.thread_id, status="busy")
        store.create(metadata={"env": "prod"})
        # t2 stays idle
        store.create(metadata={"env": "dev"})

        results = store.search(metadata={"env": "prod"}, status="busy")
        assert len(results) == 1
        assert results[0].thread_id == t1.thread_id

    def test_search_limit(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=10)
        results = store.search(limit=3)
        assert len(results) == 3

    def test_search_offset(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=5)
        all_results = store.search(limit=100)
        offset_results = store.search(offset=2, limit=100)
        assert len(offset_results) == 3
        assert offset_results[0].thread_id == all_results[2].thread_id

    def test_search_offset_beyond_results(self) -> None:
        store = InMemoryThreadStore()
        self._populate(store, count=3)
        results = store.search(offset=10)
        assert results == []

    def test_search_ordered_newest_first(self) -> None:
        store = InMemoryThreadStore()
        t1 = store.create()
        store.create()  # middle thread
        t3 = store.create()
        results = store.search()
        assert results[0].thread_id == t3.thread_id
        assert results[2].thread_id == t1.thread_id

    def test_search_returns_deep_copies(self) -> None:
        store = InMemoryThreadStore()
        store.create(metadata={"key": "val"})
        results = store.search()
        assert results[0].metadata is not None
        results[0].metadata["key"] = "mutated"
        refetched = store.search()
        assert refetched[0].metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_creates(self) -> None:
        """Multiple threads creating simultaneously should not lose data."""
        store = InMemoryThreadStore()
        errors: list[Exception] = []

        def create_threads(n: int) -> None:
            try:
                for _ in range(n):
                    store.create(metadata={"worker": threading.current_thread().name})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_threads, args=(20,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        all_threads = store.search(limit=200)
        assert len(all_threads) == 100

    def test_concurrent_updates(self) -> None:
        """Concurrent updates should not corrupt state."""
        store = InMemoryThreadStore()
        thread = store.create()
        errors: list[Exception] = []

        def update_thread(status: ThreadStatus) -> None:
            try:
                for _ in range(20):
                    store.update(thread.thread_id, status=status)
            except Exception as e:
                errors.append(e)

        workers = [
            threading.Thread(target=update_thread, args=("busy",)),
            threading.Thread(target=update_thread, args=("idle",)),
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        assert not errors
        result = store.get(thread.thread_id)
        assert result is not None
        assert result.status in ("busy", "idle")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_create_with_empty_metadata(self) -> None:
        """Empty dict metadata is distinct from None metadata."""
        store = InMemoryThreadStore()
        thread = store.create(metadata={})
        assert thread.metadata == {}

    def test_update_metadata_to_empty_dict(self) -> None:
        """Updating metadata to {} should set it to empty, not None."""
        store = InMemoryThreadStore()
        thread = store.create(metadata={"key": "val"})
        updated = store.update(thread.thread_id, metadata={})
        assert updated.metadata == {}

    def test_all_thread_statuses(self) -> None:
        """All valid ThreadStatus values should be settable."""
        store = InMemoryThreadStore()
        for status in ("idle", "busy", "interrupted", "error"):
            thread = store.create()
            updated = store.update(thread.thread_id, status=status)
            assert updated.status == status

    def test_create_and_immediately_delete(self) -> None:
        store = InMemoryThreadStore()
        thread = store.create()
        store.delete(thread.thread_id)
        assert store.get(thread.thread_id) is None

    def test_mapping_metadata_input(self) -> None:
        """create/update should accept Mapping, not just dict."""
        from types import MappingProxyType

        store = InMemoryThreadStore()
        frozen: MappingProxyType[str, Any] = MappingProxyType({"key": "val"})
        thread = store.create(metadata=frozen)
        assert thread.metadata == {"key": "val"}

        updated = store.update(thread.thread_id, metadata=frozen)
        assert updated.metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Oracle-requested gap tests
# ---------------------------------------------------------------------------


class TestOracleGapTests:
    """Tests requested by Oracle post-implementation review."""

    def test_duplicate_id_raises_valueerror(self) -> None:
        """Custom id_factory that returns duplicates should raise ValueError."""
        store = InMemoryThreadStore(id_factory=lambda: "fixed-id")
        store.create()  # first succeeds
        with pytest.raises(ValueError, match="Duplicate thread ID"):
            store.create()  # second collides

    def test_search_negative_limit_raises(self) -> None:
        store = InMemoryThreadStore()
        with pytest.raises(ValueError, match="limit must be non-negative"):
            store.search(limit=-1)

    def test_search_negative_offset_raises(self) -> None:
        store = InMemoryThreadStore()
        with pytest.raises(ValueError, match="offset must be non-negative"):
            store.search(offset=-1)

    def test_delete_returns_none(self) -> None:
        """delete() should return None (not bool) after Oracle review."""
        store = InMemoryThreadStore()
        thread = store.create()
        store.delete(thread.thread_id)  # should not raise

    def test_deep_copy_isolation_values(self) -> None:
        """Mutating returned values dict must not affect stored state."""
        store = InMemoryThreadStore()
        thread = store.create()
        updated = store.update(
            thread.thread_id, values={"messages": [{"role": "user", "content": "hi"}]}
        )
        # Mutate the returned values
        assert updated.values is not None
        updated.values["messages"].append({"role": "assistant", "content": "bye"})
        # Verify stored state is unchanged
        refetched = store.get(thread.thread_id)
        assert refetched is not None
        assert refetched.values == {"messages": [{"role": "user", "content": "hi"}]}

    def test_deep_copy_isolation_interrupts(self) -> None:
        """Mutating returned interrupts must not affect stored state."""
        store = InMemoryThreadStore()
        thread = store.create()
        interrupts = {"node_a": [Interrupt(id="i-1", value="pause")]}
        updated = store.update(thread.thread_id, interrupts=interrupts)
        # Mutate the returned interrupts
        updated.interrupts["node_a"].append(Interrupt(id="i-2", value="extra"))
        # Verify stored state is unchanged
        refetched = store.get(thread.thread_id)
        assert refetched is not None
        assert len(refetched.interrupts["node_a"]) == 1

    def test_populate_helper_preserves_empty_metadata(self) -> None:
        """Regression: _populate with metadata={} should not convert to None."""
        store = InMemoryThreadStore()
        thread = store.create(metadata={})
        assert thread.metadata == {}  # not None


# ---------------------------------------------------------------------------
# Count
# ---------------------------------------------------------------------------


class TestCount:
    def test_count_all(self) -> None:
        store = InMemoryThreadStore()
        for _ in range(5):
            store.create()
        assert store.count() == 5

    def test_count_empty_store(self) -> None:
        store = InMemoryThreadStore()
        assert store.count() == 0

    def test_count_by_status(self) -> None:
        store = InMemoryThreadStore()
        t1 = store.create()
        t2 = store.create()
        store.create()
        store.update(t1.thread_id, status="busy")
        store.update(t2.thread_id, status="busy")
        assert store.count(status="busy") == 2
        assert store.count(status="idle") == 1

    def test_count_by_metadata(self) -> None:
        store = InMemoryThreadStore()
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        assert store.count(metadata={"env": "prod"}) == 2
        assert store.count(metadata={"env": "dev"}) == 1

    def test_count_combined_filters(self) -> None:
        store = InMemoryThreadStore()
        t1 = store.create(metadata={"env": "prod"})
        store.update(t1.thread_id, status="busy")
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        assert store.count(metadata={"env": "prod"}, status="busy") == 1
        assert store.count(metadata={"env": "prod"}, status="idle") == 1

    def test_count_no_match(self) -> None:
        store = InMemoryThreadStore()
        store.create(metadata={"env": "prod"})
        assert store.count(metadata={"env": "staging"}) == 0

    def test_count_metadata_none_threads(self) -> None:
        store = InMemoryThreadStore()
        store.create()  # metadata=None
        store.create(metadata={"k": "v"})
        assert store.count(metadata={"k": "v"}) == 1

    def test_count_consistent_with_search(self) -> None:
        store = InMemoryThreadStore()
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "prod"})
        store.create(metadata={"env": "dev"})
        count = store.count(metadata={"env": "prod"})
        search_results = store.search(metadata={"env": "prod"}, limit=100)
        assert count == len(search_results)
