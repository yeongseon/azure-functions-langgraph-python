"""Local Blob ownership-guard evidence; no Azure or fencing certification."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing
import copy
import logging
from pathlib import Path
import threading
from typing import Final
import warnings

import pytest

from azure_functions_langgraph.locks import ThreadLock
from azure_functions_langgraph.locks.azure_blob import AzureBlobLeaseThreadLock
from azure_functions_langgraph.locks.guard import (
    LeaseLossReason,
    OwnershipGuard,
    OwnershipGuardProvider,
    ThreadLeaseLostError,
)
from tests.test_blob_lease_evaluation import _TimedContainer, _TimedLease
from tests.test_locks_azure_blob import (
    FakeHttpResponseError,
    MockBlobLease,
    MockContainerClient,
    _install_fake_azure_modules,
    _make_lock,
)

__all__ = ["_install_fake_azure_modules"]
pytestmark = pytest.mark.filterwarnings("ignore:.*auto_renew=False.*:UserWarning")
# SIZE_OK: the assigned regression matrix must live in this ONE new test file.
GRAPH: Final = "graph-SECRET-790"
THREAD: Final = "thread-SECRET-7f3a"
KEY: Final = (GRAPH, THREAD)
LOGGER: Final = "azure_functions_langgraph.locks.azure_blob"


@pytest.fixture
def stack() -> Iterator[ExitStack]:
    with ExitStack() as resources:
        yield resources


@pytest.fixture
def lock(stack: ExitStack) -> AzureBlobLeaseThreadLock:
    result: AzureBlobLeaseThreadLock = _make_lock(
        _TimedContainer(), auto_renew=False, guard_checkpoint_writes=True
    )
    stack.callback(result.close)
    return result


@pytest.fixture
def token(lock: AzureBlobLeaseThreadLock) -> str:
    result = lock.acquire(*KEY)
    assert result is not None
    return result


@pytest.fixture
def guard(lock: AzureBlobLeaseThreadLock, token: str) -> OwnershipGuard:
    result = lock.ownership_guard(*KEY, token)
    assert isinstance(result, OwnershipGuard)
    return result


def _reject(guard: OwnershipGuard, reason: LeaseLossReason) -> ThreadLeaseLostError:
    with pytest.raises(ThreadLeaseLostError) as caught:
        guard.check()
    error = caught.value
    assert (error.reason, error.graph_name, error.thread_id) == (reason, GRAPH, THREAD)
    assert GRAPH not in str(error)
    assert THREAD not in str(error)
    return error


def _passes(guard: OwnershipGuard) -> None:
    check: Callable[[], bool | None] = guard.check
    assert check() is None


def _lose(lock: AzureBlobLeaseThreadLock, monkeypatch: pytest.MonkeyPatch) -> None:
    def renew() -> None:
        raise FakeHttpResponseError(error_code="LeaseLost", status_code=409)

    monkeypatch.setattr(lock._active_leases[KEY].lease, "renew", renew)
    lock._renew_all_once()


# Kills: enabling guards by default or ignoring an explicit False flag.
@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit-false"])
def test_disabled_guard_preserves_capabilities_and_locking(
    stack: ExitStack, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    # Given a default lock, with the renewal worker prevented from ticking.
    monkeypatch.setattr(AzureBlobLeaseThreadLock, "_renewal_worker", lambda self: None)
    container = MockContainerClient()
    lock = (
        AzureBlobLeaseThreadLock(container_client=container, guard_checkpoint_writes=False)
        if explicit
        else AzureBlobLeaseThreadLock(container_client=container)
    )
    stack.callback(lock.close)
    token = lock.acquire(*KEY)
    assert token is not None
    # When the optional capability is requested while ownership is held.
    result = lock.ownership_guard(*KEY, token)
    # Then it is disabled without changing the existing lock protocols or exclusion.
    assert result is None
    assert isinstance(lock, OwnershipGuardProvider)
    assert isinstance(lock, ThreadLock)
    assert lock.acquire(*KEY) is None
    lock.release(*KEY, token)
    assert lock.acquire(*KEY) is not None


# Kills: caching one guard instance or returning a non-guard when enabled.
def test_enabled_provider_issues_fresh_guards(
    lock: AzureBlobLeaseThreadLock, token: str, guard: OwnershipGuard
) -> None:
    # Given a guard already issued for this acquisition.
    assert isinstance(lock, OwnershipGuardProvider)
    assert isinstance(lock, ThreadLock)
    # When the same acquisition requests another guard.
    another = lock.ownership_guard(*KEY, token)
    # Then each call owns independent reporting state and a usable guard.
    assert isinstance(another, OwnershipGuard)
    assert another is not guard
    another.check()


# Kills: rejecting the matching active token or doing service I/O during check().
def test_held_guard_passes_without_service_calls(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a held lease whose service methods must not be used by a check.
    def forbidden() -> None:
        pytest.fail("ownership checks must not contact the service")

    lease = lock._active_leases[KEY].lease
    with monkeypatch.context() as patch:
        patch.setattr(lease, "renew", forbidden)
        patch.setattr(lease, "release", forbidden)
        # When checking local ownership.
        _passes(guard)
    # Then a healthy check is a successful None-returning operation.


# Kills: exposing guard internals or the original owner token in string conversion.
@pytest.mark.parametrize("render", [repr, str, format], ids=["repr", "str", "format"])
def test_guard_representation_is_opaque(
    guard: OwnershipGuard, token: str, render: Callable[[OwnershipGuard], str]
) -> None:
    # Given a guard carrying a secret acquisition token.
    # When it is rendered through each public conversion.
    text = render(guard)
    # Then neither identity nor credentials appear in diagnostic representations.
    assert text == "<OwnershipGuard>"
    assert token not in text
    assert GRAPH not in text
    assert THREAD not in text


# Kills: copying the lock reference or issuing an independent guard during copying.
@pytest.mark.parametrize("deep", [False, True], ids=["copy", "deepcopy"])
def test_copy_preserves_guard_identity(guard: OwnershipGuard, deep: bool) -> None:
    # Given a guard embedded in a run configuration.
    # When a caller copies it.
    result = copy.deepcopy(guard) if deep else copy.copy(guard)
    # Then the original guard, including its reporting state, is shared.
    assert result is guard


# Kills: deepcopy recursively cloning a guard inside a configuration mapping.
def test_nested_deepcopy_preserves_guard_identity(guard: OwnershipGuard) -> None:
    # Given a caller-owned mapping containing the guard.
    config = {"guard": guard}
    # When the mapping is deep-copied.
    result = copy.deepcopy(config)
    # Then the mapping is independent but the guard remains shared.
    assert result is not config
    assert result["guard"] is guard


# Kills: removing slots and thereby exposing a dictionary of secret attributes.
def test_guard_has_no_attribute_dictionary(guard: OwnershipGuard) -> None:
    # Given the concrete guard returned by the provider.
    # When generic code attempts to enumerate its instance dictionary.
    # Then there is no dictionary carrying a serializable token.
    with pytest.raises(AttributeError):
        getattr(guard, "__dict__")


# Kills: accepting a token after release removes its tracking entry.
def test_released_guard_rejects(
    lock: AzureBlobLeaseThreadLock, token: str, guard: OwnershipGuard
) -> None:
    # Given a guard bound to a held acquisition.
    # When its owner releases the token.
    lock.release(*KEY, token)
    # Then the rejection carries the bound key but no client-visible secrets.
    error = _reject(guard, "released")
    assert token not in str(error)


# Kills: rebinding an old guard to a new acquisition of the same key.
def test_reacquisition_supersedes_only_the_original_guard(
    lock: AzureBlobLeaseThreadLock, token: str, guard: OwnershipGuard
) -> None:
    # Given an original acquisition that has completed.
    lock.release(*KEY, token)
    # When this same lock reacquires the key under a new token.
    replacement = lock.acquire(*KEY)
    assert replacement is not None and replacement != token
    current = lock.ownership_guard(*KEY, replacement)
    assert current is not None
    # Then only the current guard passes; the original identity is immutable.
    _reject(guard, "superseded")
    _passes(current)
    lock.release(*KEY, token)
    _passes(current)


# Kills: ignoring token mismatch, including when a mismatched entry is also lost.
@pytest.mark.parametrize("lost", [False, True])
def test_wrong_token_is_superseded_before_loss(
    lock: AzureBlobLeaseThreadLock, token: str, lost: bool
) -> None:
    # Given a tracked key and a guard carrying a different token.
    wrong = lock.ownership_guard(*KEY, token + "-foreign")
    assert wrong is not None
    lock._active_leases[KEY].lost = lost
    # When the wrong acquisition checks ownership.
    # Then mismatch takes priority over the lost bit.
    _reject(wrong, "superseded")


# Kills: ignoring the closed state after close() removes active leases.
def test_closed_guard_rejects(lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard) -> None:
    # Given an active guarded acquisition.
    # When graceful shutdown finishes.
    lock.close()
    # Then the original guard reports release, not loss or missing guard.
    _reject(guard, "released")


# Kills: reading entries before _closed, or omitting the early closed check.
@pytest.mark.parametrize("foreign", [False, True], ids=["held", "foreign-token"])
def test_close_window_takes_priority_over_present_entry(
    lock: AzureBlobLeaseThreadLock, token: str, foreign: bool
) -> None:
    # Given close() has set its flag but not yet cleared this healthy entry.
    guard = lock.ownership_guard(*KEY, token + "-foreign" if foreign else token)
    assert guard is not None
    assert KEY in lock._active_leases and not lock._active_leases[KEY].lost
    lock._closed = True
    try:
        # When a checkpoint observes that intermediate shutdown state.
        # Then it cannot accept the still-present token or report supersession.
        _reject(guard, "released")
    finally:
        lock._closed = False


# Kills: failing to map any definitive renew error to guard-visible lost ownership.
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("LeaseLost", 409),
        ("LeaseIdMismatchWithLeaseOperation", 409),
        ("LeaseNotPresentWithLeaseOperation", 409),
        ("LeaseIdMissing", 409),
        (None, 412),
    ],
)
def test_definitive_renewal_failure_rejects_immediately(
    lock: AzureBlobLeaseThreadLock,
    guard: OwnershipGuard,
    monkeypatch: pytest.MonkeyPatch,
    code: str | None,
    status: int,
) -> None:
    # Given an Azure renewal response that definitively ends this acquisition.
    def renew() -> None:
        raise FakeHttpResponseError(error_code=code, status_code=status)

    monkeypatch.setattr(lock._active_leases[KEY].lease, "renew", renew)
    # When the first renewal tick observes the failure.
    lock._renew_all_once()
    # Then writes are rejected immediately, with acquisition-bound diagnostics.
    _reject(guard, "lost")


# Kills: reviving lost ownership on a later check or a successful renewal tick.
def test_detected_loss_is_sticky_across_checks_and_ticks(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given ownership was definitively lost on renewal.
    _lose(lock, monkeypatch)
    lease = lock._active_leases[KEY].lease
    assert isinstance(lease, _TimedLease)
    monkeypatch.setattr(lease, "renew", lambda: None)
    # When further checks and renewal ticks occur.
    for _ in range(3):
        lock._renew_all_once()
        # Then even a service method that now succeeds cannot revive the guard.
        _reject(guard, "lost")


# Kills: keeping lost as the reason after release, or retaining the occupied key.
def test_release_after_loss_reports_released_and_allows_reacquisition(
    lock: AzureBlobLeaseThreadLock,
    token: str,
    guard: OwnershipGuard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the service lease is gone and the next tick detects that fact.
    state = lock._active_leases[KEY]
    state.lease.release()
    _lose(lock, monkeypatch)
    _reject(guard, "lost")
    # When the original owner completes cleanup.
    lock.release(*KEY, token)
    # Then entry removal takes precedence over its former sticky lost bit.
    _reject(guard, "released")
    assert lock.acquire(*KEY) is not None


# Kills: rejecting before the third transient miss, or failing to reject at three.
@pytest.mark.parametrize("ticks", [1, 2, 3])
def test_transient_loss_threshold(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard, ticks: int
) -> None:
    # Given a timed fake with transient transport failures rather than lease errors.
    lease = lock._active_leases[KEY].lease
    assert isinstance(lease, _TimedLease)
    lease.renew_failure = True
    # When exactly the specified number of consecutive renewals fail.
    for _ in range(ticks):
        lock._renew_all_once()
    # Then only the third miss is a detected loss.
    if ticks < 3:
        _passes(guard)
    else:
        _reject(guard, "lost")


# Kills: accumulating failures across successful renewals rather than resetting.
def test_success_resets_transient_failure_budget(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard
) -> None:
    # Given two transient misses followed by one healthy renewal.
    lease = lock._active_leases[KEY].lease
    assert isinstance(lease, _TimedLease)
    lease.renew_failure = True
    lock._renew_all_once()
    lock._renew_all_once()
    lease.renew_failure = False
    lock._renew_all_once()
    lease.renew_failure = True
    # When two more transient misses occur.
    lock._renew_all_once()
    lock._renew_all_once()
    # Then they are not treated as the third and fourth consecutive misses.
    _passes(guard)


# Kills: inferring expired ownership from last_successful_renewal or finite TTL.
@pytest.mark.parametrize("duration", [60, -1], ids=["finite", "infinite"])
def test_old_renewal_timestamp_does_not_infer_loss(stack: ExitStack, duration: int) -> None:
    # Given a finite or infinite lease with no automatic renewal.
    lock: AzureBlobLeaseThreadLock = _make_lock(
        _TimedContainer(), auto_renew=False, lease_duration=duration, guard_checkpoint_writes=True
    )
    stack.callback(lock.close)
    token = lock.acquire(*KEY)
    assert token is not None
    guard = lock.ownership_guard(*KEY, token)
    assert guard is not None
    # When the local successful-renewal timestamp is arbitrarily old.
    lock._active_leases[KEY].last_successful_renewal = -1e9
    # Then the guard reports only detected local state, not clock speculation.
    _passes(guard)


# Kills: looking up ownership by only graph/thread, or sharing loss across keys.
@pytest.mark.parametrize("other", [(GRAPH, "other-thread"), ("other-graph", THREAD)])
def test_loss_is_isolated_to_the_bound_key(
    lock: AzureBlobLeaseThreadLock,
    guard: OwnershipGuard,
    monkeypatch: pytest.MonkeyPatch,
    other: tuple[str, str],
) -> None:
    # Given another held key differing in exactly one coordinate.
    assert lock.acquire(*other) is not None

    def renew() -> None:
        raise FakeHttpResponseError(error_code="LeaseLost", status_code=409)

    monkeypatch.setattr(lock._active_leases[other].lease, "renew", renew)
    # When only that other key loses its lease.
    lock._renew_all_once()
    # Then this acquisition remains acceptable.
    assert lock._active_leases[other].lost
    _passes(guard)


# Kills: taking _active_leases_guard in either the passing or rejecting check path.
@pytest.mark.parametrize("lost", [False, True])
def test_check_completes_on_another_thread_while_mutex_is_held(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard, lost: bool
) -> None:
    # Given the actual bookkeeping mutex is held by this test thread.
    lock._active_leases[KEY].lost = lost
    outcomes: list[ThreadLeaseLostError | None] = []

    def check() -> None:
        try:
            guard.check()
        except ThreadLeaseLostError as error:
            outcomes.append(error)
        else:
            outcomes.append(None)

    worker = threading.Thread(target=check)
    # When another thread checks while the real mutex remains unavailable.
    try:
        with lock._active_leases_guard:
            worker.start()
            worker.join(timeout=5)
            finished_under_mutex = not worker.is_alive()
    finally:
        if worker.ident is not None:
            worker.join(timeout=5)
    # Then the check completed without waiting for the mutex to be released.
    assert not worker.is_alive()
    assert finished_under_mutex
    assert len(outcomes) == 1
    if lost:
        error = outcomes[0]
        assert isinstance(error, ThreadLeaseLostError)
        assert (error.reason, error.graph_name, error.thread_id) == ("lost", GRAPH, THREAD)
    else:
        assert outcomes == [None]


# Kills: omitting the first warning, leaking tokens, or warning on every rejection.
def test_rejections_warn_once_then_log_debug(
    lock: AzureBlobLeaseThreadLock,
    token: str,
    guard: OwnershipGuard,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a released guard and WARNING-level capture of only its logger.
    lock.release(*KEY, token)
    caplog.set_level(logging.WARNING, logger=LOGGER)
    # When it rejects its first operation.
    _reject(guard, "released")
    # Then exactly one warning describes the key and reason, never the token.
    records = [record for record in caplog.records if record.name == LOGGER]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    message = records[0].getMessage()
    assert all(value in message for value in (GRAPH, THREAD, "released"))
    assert token not in caplog.text
    # When two more writes are attempted with DEBUG capture enabled.
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _reject(guard, "released")
    _reject(guard, "released")
    # Then repeated failures remain observable without warning-level flooding.
    records = [record for record in caplog.records if record.name == LOGGER]
    assert [record.levelno for record in records] == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    assert token not in caplog.text


# Kills: sharing the reported flag between acquisitions or warning on successful checks.
def test_new_acquisition_warns_independently_and_success_is_silent(
    lock: AzureBlobLeaseThreadLock,
    token: str,
    guard: OwnershipGuard,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an earlier guard has already reported its released acquisition.
    lock.release(*KEY, token)
    _reject(guard, "released")
    replacement = lock.acquire(*KEY)
    assert replacement is not None
    current = lock.ownership_guard(*KEY, replacement)
    assert current is not None
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    # When a fresh acquisition first checks successfully, then becomes released.
    current.check()
    assert caplog.records == []
    lock.release(*KEY, replacement)
    caplog.clear()
    _reject(current, "released")
    # Then this guard has its own first-warning budget.
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert replacement not in caplog.text


# Kills: wrong guard-warning predicate, duplicate warnings, or incorrect stacklevel.
@pytest.mark.parametrize(
    ("enabled", "auto_renew", "duration", "expected"),
    [(True, False, 60, 1), (True, True, -1, 1), (True, True, 60, 0), (False, False, 60, 0)],
    ids=["finite-unrenewed", "infinite", "finite-renewed", "disabled"],
)
def test_constructor_guard_warning_targets_the_caller(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, auto_renew: bool, duration: int, expected: int
) -> None:
    # Given background ticks are disabled even when testing default renewal settings.
    monkeypatch.setattr(AzureBlobLeaseThreadLock, "_renewal_worker", lambda self: None)
    # When constructing directly, so stacklevel must resolve to this file.
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always")
        with closing(
            AzureBlobLeaseThreadLock(
                container_client=MockContainerClient(),
                guard_checkpoint_writes=enabled,
                auto_renew=auto_renew,
                lease_duration=duration,
            )
        ):
            pass
    # Then the additional guard warning is distinct from the existing TTL warning.
    matching = [
        record for record in records if "guard_checkpoint_writes=True" in str(record.message)
    ]
    assert len(matching) == expected
    assert all(record.category is UserWarning for record in matching)
    assert all(Path(record.filename).resolve() == Path(__file__).resolve() for record in matching)
    finite_warnings = [record for record in records if "is finite" in str(record.message)]
    assert len(finite_warnings) == int(duration != -1 and not auto_renew)


# Kills: changing contention/release semantics when the guard flag is enabled.
@pytest.mark.parametrize("enabled", [False, True])
def test_guard_flag_preserves_contention_and_release(stack: ExitStack, enabled: bool) -> None:
    # Given two independent instances sharing the same marker service.
    container = MockContainerClient()
    first: AzureBlobLeaseThreadLock = _make_lock(
        container, auto_renew=False, guard_checkpoint_writes=enabled
    )
    stack.callback(first.close)
    second: AzureBlobLeaseThreadLock = _make_lock(
        container, auto_renew=False, guard_checkpoint_writes=enabled
    )
    stack.callback(second.close)
    token = first.acquire(*KEY)
    assert token is not None
    # When a local and a remote contender try before the owner releases.
    assert first.acquire(*KEY) is None
    assert second.acquire(*KEY) is None
    first.release(*KEY, token)
    # Then ordinary release permits another instance to acquire unchanged.
    assert second.acquire(*KEY) is not None


# Kills: freeing a lost key early or releasing its already-lost service lease.
@pytest.mark.parametrize("enabled", [False, True])
def test_guard_flag_preserves_lost_entry_cleanup(
    stack: ExitStack, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    # Given a definitive loss with a service-release call recorder.
    lock: AzureBlobLeaseThreadLock = _make_lock(
        MockContainerClient(), auto_renew=False, guard_checkpoint_writes=enabled
    )
    stack.callback(lock.close)
    token = lock.acquire(*KEY)
    assert token is not None
    _lose(lock, monkeypatch)
    releases: list[str] = []
    monkeypatch.setattr(
        lock._active_leases[KEY].lease, "release", lambda: releases.append("called")
    )
    # When the occupied key is retried and then the original owner releases it.
    assert lock.acquire(*KEY) is None
    lock.release(*KEY, token)
    # Then cleanup never calls Azure for the lost lease and clears local exclusion.
    assert releases == []
    assert KEY not in lock._active_leases


# Kills: leaking active service leases during close when guarding is enabled.
@pytest.mark.parametrize("enabled", [False, True])
def test_guard_flag_preserves_close_cleanup(stack: ExitStack, enabled: bool) -> None:
    # Given two healthy leases on different keys.
    lock: AzureBlobLeaseThreadLock = _make_lock(
        MockContainerClient(), auto_renew=False, guard_checkpoint_writes=enabled
    )
    stack.callback(lock.close)
    assert lock.acquire(*KEY) is not None
    assert lock.acquire(GRAPH, "second-thread") is not None
    leases = [state.lease for state in lock._active_leases.values()]
    assert all(isinstance(lease, MockBlobLease) for lease in leases)
    # When the lock shuts down.
    lock.close()
    # Then all actual service leases are released, independent of the flag.
    for lease in leases:
        assert isinstance(lease, MockBlobLease)
        assert lease.released
    assert not lock._active_leases


# Kills: non-reentrant guard state or spurious rejection during concurrent healthy checks.
def test_eight_threads_can_check_healthy_ownership(guard: OwnershipGuard) -> None:
    # Given a passing guard shared by eight independent writers.
    counts = [0] * 8
    errors: list[ThreadLeaseLostError] = []

    def check(index: int) -> None:
        for _ in range(500):
            try:
                guard.check()
            except ThreadLeaseLostError as error:
                errors.append(error)
                return
            counts[index] += 1

    workers = [threading.Thread(target=check, args=(index,)) for index in range(8)]
    # When all writers perform their checks with no ownership transition.
    try:
        for worker in workers:
            worker.start()
    finally:
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)
    # Then all 4,000 checks completed and no writer saw an exception.
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert counts == [500] * 8


# Kills: stale cached success or revival after a thread has observed lost ownership.
def test_concurrent_checks_observe_sticky_loss(
    lock: AzureBlobLeaseThreadLock, guard: OwnershipGuard
) -> None:
    # Given each writer checks before loss, then waits for a coordinated transition.
    ready = [threading.Event() for _ in range(8)]
    changed = threading.Event()
    outcomes: list[list[LeaseLossReason | None]] = [[] for _ in range(8)]
    errors: list[str] = []

    def check(index: int) -> None:
        for step in range(500):
            if step == 1:
                ready[index].set()
                if not changed.wait(timeout=5):
                    errors.append("loss transition timed out")
                    return
            try:
                guard.check()
            except ThreadLeaseLostError as error:
                outcomes[index].append(error.reason)
                if (error.graph_name, error.thread_id) != KEY:
                    errors.append("guard reported a different key")
            else:
                outcomes[index].append(None)

    workers = [threading.Thread(target=check, args=(index,)) for index in range(8)]
    # When the main thread marks the entry lost while all writers are active.
    try:
        for worker in workers:
            worker.start()
        assert all(event.wait(timeout=5) for event in ready)
        lock._active_leases[KEY].lost = True
    finally:
        changed.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=5)
    # Then every writer observes success before loss, and only lost afterwards.
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert outcomes == [[None] + ["lost"] * 499 for _ in range(8)]


# Kills: a signature drift between AzureBlobLeaseThreadLock.ownership_guard and the
# OwnershipGuardProvider protocol (mypy --strict proves the structural match).
def test_lock_conforms_to_the_provider_protocol_statically(lock: AzureBlobLeaseThreadLock) -> None:
    # Given the shipped lock, typed as the optional capability third-party locks implement.
    provider: OwnershipGuardProvider = lock
    # Then the assignment above type-checks, and the object is the same lock at runtime.
    assert provider is lock
