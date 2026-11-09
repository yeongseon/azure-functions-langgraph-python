"""Concurrent metadata changes to one thread must not erase each other (#806).

``PATCH /threads/{id}`` read the thread, merged the request into its copy and wrote the whole map
back, and the run handlers did the same for the metadata a run carries. Anything another request
added between that read and the write was silently dropped, although that request had been
acknowledged: no ETag conflict is even needed, because the stale map replaces the stored one. The
bundled stores now merge atomically through the optional ``MetadataMergeProvider`` capability
(``merge_metadata``), and the handlers use it. Fake-backed: interleavings are forced
deterministically; the Azurite lane runs the same questions against the real Table service.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import threading
from typing import Any, cast

import azure.functions as func
import pytest

from azure_functions_langgraph.app import LangGraphApp
from azure_functions_langgraph.platform.contracts import Thread
from azure_functions_langgraph.platform.stores import InMemoryThreadStore, MetadataMergeProvider
from tests.conftest import FakeCompiledGraph
from tests.test_platform_routes import _get_fn, _patch_request, _post_request
from tests.test_stores_azure_table import (
    FakeResourceModifiedError,
    _new_store,
)

SECRET = "private-metadata-canary"


class PlainStore:
    """A custom store that implements only the ``ThreadStore`` protocol: no atomic merge."""

    FORWARDED = (
        "create",
        "get",
        "update",
        "delete",
        "search",
        "count",
        "try_acquire_run_lock",
        "release_run_lock",
        "renew_run_lock",
    )

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        for name in self.FORWARDED:
            setattr(self, name, getattr(inner, name))


@pytest.fixture(params=["memory", "table"])
def store(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryThreadStore()
    return _new_store()[0]


# ---------------------------------------------------------------------------
# The stores
# ---------------------------------------------------------------------------


def test_both_bundled_stores_provide_the_capability(store: Any) -> None:
    assert isinstance(store, MetadataMergeProvider)


def test_the_patch_is_merged_into_the_current_metadata(store: Any) -> None:
    # Given a thread with metadata.
    thread = store.create(metadata={"base": 0, "shared": "old"})

    # When two different patches are merged one after the other, one of them overriding a key.
    store.merge_metadata(thread.thread_id, {"a": 1})
    merged = store.merge_metadata(thread.thread_id, {"b": 2, "shared": "new"})

    # Then every key survives, the overridden key has the last writer's value, and the returned
    # thread is the stored one.
    assert merged.metadata == {"base": 0, "shared": "new", "a": 1, "b": 2}
    assert store.get(thread.thread_id) == merged


def test_a_thread_without_metadata_gets_the_patch(store: Any) -> None:
    thread = store.create()

    merged = store.merge_metadata(thread.thread_id, {"a": 1})

    assert merged.metadata == {"a": 1}


def test_everything_else_in_the_thread_is_preserved(store: Any) -> None:
    # Given a busy thread bound to an assistant, with values.
    thread = store.create(metadata={"base": 0})
    held = store.try_acquire_run_lock(thread.thread_id, assistant_id="agent")
    assert held is not None
    store.update(thread.thread_id, values={"progress": 1})
    before = store.get(thread.thread_id)

    # When metadata is merged.
    merged = store.merge_metadata(thread.thread_id, {"a": 1})

    # Then status, binding, values and the lock token and expiry are untouched.
    assert (merged.status, merged.assistant_id, merged.values) == ("busy", "agent", {"progress": 1})
    assert (merged.lock_token, merged.lock_expires_at) == (
        before.lock_token,
        before.lock_expires_at,
    )
    assert merged.metadata == {"base": 0, "a": 1}


def test_a_missing_thread_raises_keyerror(store: Any) -> None:
    with pytest.raises(KeyError):
        store.merge_metadata("missing-thread", {"a": 1})


def test_the_callers_mapping_is_not_changed_or_aliased() -> None:
    # Given a nested value in the patch.
    store = InMemoryThreadStore()
    thread = store.create()
    patch: dict[str, Any] = {"nested": {"k": [1]}}

    # When it is merged and the caller then mutates its own mapping.
    store.merge_metadata(thread.thread_id, patch)
    patch["nested"]["k"].append(2)
    patch["other"] = 1

    # Then the stored metadata does not follow it.
    stored = store.get(thread.thread_id)
    assert stored is not None and stored.metadata == {"nested": {"k": [1]}}


def test_concurrent_merges_of_different_keys_all_survive() -> None:
    # Given many threads that each merge their own key into the same thread at the same time.
    store = InMemoryThreadStore()
    thread = store.create(metadata={"base": 0})
    workers = 24
    barrier = threading.Barrier(workers)

    def merge(index: int) -> None:
        barrier.wait()
        store.merge_metadata(thread.thread_id, {f"key-{index}": index})

    pool = [threading.Thread(target=merge, args=(i,)) for i in range(workers)]

    # When they run.
    for t in pool:
        t.start()
    for t in pool:
        t.join(10)

    # Then no key was lost.
    stored = store.get(thread.thread_id)
    assert stored is not None and stored.metadata is not None
    assert set(stored.metadata) == {"base", *(f"key-{i}" for i in range(workers))}


def test_an_overridden_update_still_sees_the_in_memory_merge() -> None:
    # Given a store subclass that audits metadata writes through update (the pattern the
    # platform tests already use for fault injection).
    seen: list[dict[str, Any]] = []

    class Auditing(InMemoryThreadStore):
        def update(self, thread_id: str, **kwargs: Any) -> Thread:
            if kwargs.get("metadata") is not None:
                seen.append(dict(kwargs["metadata"]))
            return super().update(thread_id, **kwargs)

    store = Auditing()
    thread = store.create(metadata={"base": 0})

    # When metadata is merged.
    store.merge_metadata(thread.thread_id, {"a": 1})

    # Then the override saw exactly one write, carrying the merged map.
    assert seen == [{"base": 0, "a": 1}]


class TestTableCompareAndSwap:
    """The Table store derives the merge from the row it just read, on every attempt."""

    def test_a_conflict_is_retried_against_the_fresh_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Given a merge whose conditional write loses the race to another writer's new key.
        store, table = _new_store()
        thread = store.create(metadata={"base": 0})
        original = table.update_entity
        raced: list[int] = []

        def update_entity(entity: dict[str, Any], mode: str, **kwargs: Any) -> None:
            if not raced:
                raced.append(1)
                store.update(thread.thread_id, metadata={"base": 0, "other-writer": True})
            original(entity, mode, **kwargs)

        monkeypatch.setattr(table, "update_entity", update_entity)

        # When it is retried.
        merged = store.merge_metadata(thread.thread_id, {"mine": 1})

        # Then the retry merged into the fresh row, so the other writer's key is kept.
        assert raced == [1]
        assert merged.metadata == {"base": 0, "other-writer": True, "mine": 1}
        assert store.get(thread.thread_id) == merged

    def test_exhausted_conflicts_raise_after_three_attempts_and_write_nothing(self) -> None:
        # Given a row that always conflicts.
        store, table = _new_store()
        thread = store.create(metadata={"base": 0})
        table.always_modified_error = True
        attempts = table.update_attempts

        # When the merge gives up.
        with pytest.raises(FakeResourceModifiedError):
            store.merge_metadata(thread.thread_id, {"a": 1})

        # Then it tried three times, like update(), and the stored metadata is unchanged.
        assert table.update_attempts - attempts == 3
        table.always_modified_error = False
        stored = store.get(thread.thread_id)
        assert stored is not None and stored.metadata == {"base": 0}

    @pytest.mark.parametrize("phase", ["read", "write"])
    def test_a_thread_deleted_mid_merge_is_a_keyerror(
        self, phase: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, table = _new_store()
        thread = store.create(metadata={"base": 0})
        if phase == "read":
            store.delete(thread.thread_id)
        else:
            original = table.update_entity

            def delete_then_write(entity: dict[str, Any], mode: str, **kwargs: Any) -> None:
                store.delete(thread.thread_id)
                original(entity, mode, **kwargs)

            monkeypatch.setattr(table, "update_entity", delete_then_write)

        with pytest.raises(KeyError):
            store.merge_metadata(thread.thread_id, {"a": 1})

    def test_large_metadata_is_split_across_properties_and_merged_back(self) -> None:
        # Given metadata that already needs several Table properties.
        store, table = _new_store()
        thread = store.create(metadata={"blob": "x" * 70_000})

        # When another key is merged in.
        merged = store.merge_metadata(thread.thread_id, {"small": 1})

        # Then the large value is intact next to the new key.
        assert merged.metadata is not None
        assert merged.metadata["small"] == 1 and len(merged.metadata["blob"]) == 70_000
        assert ("thread", thread.thread_id) in table.entities


# ---------------------------------------------------------------------------
# The handlers
# ---------------------------------------------------------------------------


class App:
    """A Platform app over any thread store, with PATCH and run handlers."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.app = LangGraphApp(platform_compat=True)
        self.app._thread_store = store
        self.app.register(graph=FakeCompiledGraph(), name="agent")
        self.thread_id = store.create(metadata={"base": 0}).thread_id
        self.patch_handler = _get_fn(self.app.function_app, "aflg_platform_threads_update")

    def patch(self, metadata: dict[str, Any]) -> func.HttpResponse:
        request = _patch_request(
            f"/api/threads/{self.thread_id}", {"metadata": metadata}, thread_id=self.thread_id
        )
        return cast(func.HttpResponse, self.patch_handler(request))

    def run(self, endpoint: str, metadata: dict[str, Any]) -> func.HttpResponse:
        handler = _get_fn(self.app.function_app, f"aflg_platform_runs_{endpoint}")
        request = _post_request(
            f"/api/threads/{self.thread_id}/runs/{endpoint}",
            {"assistant_id": "agent", "input": {}, "metadata": metadata},
            thread_id=self.thread_id,
        )
        return cast(func.HttpResponse, handler(request))

    def after_the_handlers_read(self, store_attr: str, concurrent: Callable[[], object]) -> None:
        """Run ``concurrent`` once, right after the next call of ``store.<store_attr>`` returns.

        For ``get`` that is right after a handler read the thread and before it wrote; for
        ``try_acquire_run_lock`` it is the same window in the run handlers.
        """
        real = getattr(self.store, store_attr)
        fired: list[int] = []

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = real(*args, **kwargs)
            if not fired:
                fired.append(1)
                setattr(self.store, store_attr, real)
                concurrent()
            return result

        setattr(self.store, store_attr, wrapped)

    def metadata(self) -> dict[str, Any] | None:
        stored = self.store.get(self.thread_id)
        assert stored is not None
        return cast("dict[str, Any] | None", stored.metadata)


def _app(kind: str) -> App:
    return App(InMemoryThreadStore() if kind == "memory" else _new_store()[0])


@pytest.mark.parametrize("kind", ["memory", "table"])
def test_two_concurrent_patches_of_different_keys_both_survive(kind: str) -> None:
    # Given request A that has read the thread and not yet written.
    app = _app(kind)
    app.after_the_handlers_read("get", lambda: app.patch({"b": 2}))

    # When request B patches another key and finishes first, and then A writes.
    assert app.patch({"a": 1}).status_code == 200

    # Then neither acknowledged change was lost.
    assert app.metadata() == {"base": 0, "a": 1, "b": 2}


@pytest.mark.parametrize("kind", ["memory", "table"])
def test_for_one_key_the_last_writer_wins(kind: str) -> None:
    app = _app(kind)
    app.after_the_handlers_read("get", lambda: app.patch({"shared": "second"}))

    assert app.patch({"shared": "first"}).status_code == 200

    # A's write lands last, so its value stands for the key both patched.
    assert app.metadata() == {"base": 0, "shared": "first"}


@pytest.mark.parametrize("endpoint", ["wait", "stream"])
@pytest.mark.parametrize("kind", ["memory", "table"])
def test_a_patch_during_a_runs_metadata_merge_is_not_lost(kind: str, endpoint: str) -> None:
    # Given a run that read the thread and is about to take its lock and merge its metadata.
    app = _app(kind)
    app.after_the_handlers_read("try_acquire_run_lock", lambda: app.patch({"patched": True}))

    # When a PATCH lands in that window and the run then writes its own metadata.
    assert app.run(endpoint, {"from-run": 1}).status_code == 200

    # Then both survive.
    assert app.metadata() == {"base": 0, "patched": True, "from-run": 1}


@pytest.mark.parametrize("kind", ["memory", "table"])
def test_patch_returns_the_merged_thread(kind: str) -> None:
    app = _app(kind)

    response = app.patch({"a": 1})

    assert response.status_code == 200
    assert json.loads(response.get_body())["metadata"] == {"base": 0, "a": 1}


@pytest.mark.parametrize("kind", ["memory", "table"])
def test_patching_a_thread_deleted_in_the_meantime_is_404(kind: str) -> None:
    app = _app(kind)
    app.after_the_handlers_read("get", lambda: app.store.delete(app.thread_id))

    response = app.patch({"a": 1})

    assert response.status_code == 404


def test_a_failing_merge_is_a_sanitized_500_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a store whose atomic merge fails with backend text in the exception.
    app = _app("memory")

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app.store, "merge_metadata", failing)

    # When the thread is patched.
    with caplog.at_level(logging.ERROR):
        response = app.patch({"a": 1})

    # Then the client gets a generic 500 and the cause stays in the log.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {"detail": "Internal error updating thread metadata"}
    assert SECRET.encode() not in response.get_body()
    assert SECRET in caplog.text


def test_exhausted_table_conflicts_are_a_sanitized_500() -> None:
    # Given a Table row whose conditional writes keep conflicting.
    store, table = _new_store()
    app = App(store)
    table.always_modified_error = True

    # When it is patched.
    response = app.patch({"a": 1})

    # Then the SDK's conflict error does not escape as a raw exception.
    assert response.status_code == 500
    assert json.loads(response.get_body()) == {"detail": "Internal error updating thread metadata"}


# ---------------------------------------------------------------------------
# A custom store without the capability keeps the old path
# ---------------------------------------------------------------------------


def test_a_store_without_the_capability_still_merges_through_update() -> None:
    plain = PlainStore(InMemoryThreadStore())
    assert not isinstance(plain, MetadataMergeProvider)
    app = App(plain)

    assert app.patch({"a": 1}).status_code == 200

    assert app.metadata() == {"base": 0, "a": 1}


def test_a_store_without_the_capability_can_still_lose_a_concurrent_key_documented_limit() -> None:
    # Given the same interleaving as for the bundled stores, on a store that cannot merge
    # atomically.
    app = App(PlainStore(InMemoryThreadStore()))
    app.after_the_handlers_read("get", lambda: app.patch({"b": 2}))

    assert app.patch({"a": 1}).status_code == 200

    # Then the stale map replaces the stored one: the documented limit of the fallback, which a
    # store closes by implementing merge_metadata.
    assert app.metadata() == {"base": 0, "a": 1}
