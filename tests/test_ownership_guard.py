"""Pure ownership-guard contracts; no Azure service or transport harness."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
import pickle
from queue import Queue
import threading
from typing import Final, cast

import pytest

import azure_functions_langgraph as package
from azure_functions_langgraph import locks
from azure_functions_langgraph.locks import (
    InProcessThreadLock,
    OwnershipGuard,
    OwnershipGuardProvider,
    ThreadLeaseLostError,
    ThreadLock,
)
from azure_functions_langgraph.locks import guard as guard_module
from azure_functions_langgraph.locks.guard import (
    OWNERSHIP_GUARD_CONFIG_KEY as KEY,
)
from azure_functions_langgraph.locks.guard import (
    LeaseLossReason,
    bind_ownership_guard,
    check_ownership_guard,
    config_with_ownership_guard,
    guard_run_config,
    ownership_guard_for,
    resolve_ownership_guard,
)
from tests.ownership_guard_support import GuardableLock, ScriptedGuard

# SIZE_OK: the requested unit matrix must live in ONE new file, not shared helpers.
# asyncio is intentional: these tests distinguish its context-propagation contracts.
# object-valued dictionaries model this module's deliberately untrusted config boundary.
REASONS: Final[tuple[LeaseLossReason, ...]] = ("lost", "released", "superseded", "missing")
GRAPH: Final = "graph-SECRET"
THREAD: Final = "thread-SECRET"
TOKEN: Final = "f92dc576-4a56-43aa-86d5-621da137d190"


class RecordingProvider:
    """Record capability calls, including deliberately invalid provider results."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    # Four arguments including self are required by the provider protocol.
    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> object:
        self.calls.append((graph_name, thread_id, token))
        return self.result


class Fault:
    """Inject the same exception object at either guard/provider boundary."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def check(self) -> None:
        raise self.error

    def ownership_guard(self, graph_name: str, thread_id: str, token: str) -> OwnershipGuard:
        raise self.error


# Kills: wrong reason/diagnostic attributes, dynamic messages, or conflated reason messages.
@pytest.mark.parametrize("reason", REASONS)
def test_error_has_fixed_redacted_reason_message(reason: LeaseLossReason) -> None:
    # Given sensitive diagnostic fields, including a token-looking value.
    default = ThreadLeaseLostError(reason=reason)
    # When constructing the same reason with diagnostics.
    error = ThreadLeaseLostError(reason=reason, graph_name=GRAPH, thread_id=f"{THREAD}/{TOKEN}")
    # Then diagnostics survive only in attributes, never in the public message.
    assert isinstance(error, RuntimeError)
    assert error.reason == reason
    assert error.graph_name == GRAPH
    assert error.thread_id == f"{THREAD}/{TOKEN}"
    assert default.graph_name is None and default.thread_id is None
    assert str(error) == str(default)
    assert error.args == (str(error),)
    assert all(secret not in str(error) for secret in (GRAPH, THREAD, TOKEN))
    messages = {str(ThreadLeaseLostError(reason=value)) for value in REASONS}
    assert len(messages) == 4
    if reason != "missing":
        assert "locally detected" in str(error)
        assert "not proof that a stale write" in str(error)
    else:
        assert "requires a thread lock ownership guard" in str(error)
    # Each reason says only its own thing: a missing guard is not "detected as lost".
    phrases = {
        "lost": "detected as lost",
        "released": "already released",
        "superseded": "newer execution",
        "missing": "requires a thread lock ownership guard",
    }
    for other, phrase in phrases.items():
        assert (phrase in str(error)) is (other == reason), (reason, other)


# Kills: losing keyword-only exception reconstruction or dropping diagnostic fields.
@pytest.mark.parametrize("reason", REASONS)
def test_error_copy_and_pickle_round_trip(reason: LeaseLossReason) -> None:
    # Given an error with non-default diagnostics.
    error = ThreadLeaseLostError(reason=reason, graph_name=GRAPH, thread_id=THREAD)
    # When rebuilding through all three public serialization paths.
    rebuilt = (copy.copy(error), copy.deepcopy(error), pickle.loads(pickle.dumps(error)))
    # Then each is the same exception class with the complete original payload.
    for result in rebuilt:
        assert type(result) is ThreadLeaseLostError
        assert result is not error
        assert (result.reason, result.graph_name, result.thread_id) == (reason, GRAPH, THREAD)
        assert str(result) == str(error)
        assert result.args == error.args


# Kills: allowing positional construction of the keyword-only error.
def test_error_rejects_positional_reason() -> None:
    # Given a deliberately mistyped caller to exercise the runtime contract.
    constructor = cast(Callable[[str], ThreadLeaseLostError], ThreadLeaseLostError)
    # When supplying a positional reason, then Python rejects the call.
    with pytest.raises(TypeError):
        constructor("lost")


# Kills: custom exception machinery suppressing or replacing explicit causes.
def test_error_preserves_explicit_chaining() -> None:
    # Given an original cause and the error to raise.
    cause = ValueError("underlying failure")
    error = ThreadLeaseLostError(reason="lost")
    # When explicitly chaining the ownership error.
    with pytest.raises(ThreadLeaseLostError) as caught:
        raise error from cause
    # Then the original error and cause remain observable unchanged.
    assert caught.value is error
    assert caught.value.__cause__ is cause
    assert caught.value.__suppress_context__ is True


# Kills: non-runtime protocols, extra required members, or accidental in-process opt-in.
def test_protocols_recognize_optional_capability_structurally() -> None:
    # Given a structural guard, a capable lock, and the default lock.
    guard, capable, default = ScriptedGuard(), GuardableLock(), InProcessThreadLock()
    # When probing the runtime protocols, then only supported capabilities match.
    assert isinstance(guard, OwnershipGuard)
    assert not isinstance(object(), OwnershipGuard)
    assert isinstance(capable, OwnershipGuardProvider)
    assert isinstance(capable, ThreadLock)
    assert isinstance(default, ThreadLock)
    assert not isinstance(default, OwnershipGuardProvider)


# Kills: missing/different public re-exports or exposing internal adapter plumbing.
def test_exports_keep_protocols_public_and_plumbing_private() -> None:
    # Given the intended public types and internal adapter names.
    public = (OwnershipGuard, OwnershipGuardProvider, ThreadLeaseLostError)
    internal = (
        "guard_run_config",
        "config_with_ownership_guard",
        "check_ownership_guard",
        "ownership_guard_for",
        "resolve_ownership_guard",
        "bind_ownership_guard",
        "OWNERSHIP_GUARD_CONFIG_KEY",
    )
    # When inspecting package exports, then imports retain identity and privacy.
    for value in public:
        assert value.__name__ in locks.__all__
        assert getattr(locks, value.__name__) is value
        assert getattr(guard_module, value.__name__) is value
    for name in internal:
        assert name not in guard_module.__all__
        assert name not in locks.__all__
        assert name not in package.__all__


# Kills: changing the key to one LangGraph would expose in debug/checkpoint streams.
def test_guard_key_uses_langgraph_private_pregel_namespace() -> None:
    # Given the adapter's run-config key, when checked, then it is LangGraph-private.
    assert KEY.startswith("__pregel_")
    assert KEY == "__pregel_azfunc_ownership_guard"


# Kills: calling providers without a real token or without a thread identifier.
@pytest.mark.parametrize("thread_id,token", [(THREAD, None), (THREAD, ""), (None, TOKEN)])
def test_provider_is_not_called_without_acquisition(
    thread_id: str | None, token: str | None
) -> None:
    # Given a provider that would return a guard if incorrectly called.
    provider = RecordingProvider(ScriptedGuard())
    # When no acquisition exists.
    result = ownership_guard_for(provider, GRAPH, thread_id, token)
    # Then guarding is disabled without consulting the provider.
    assert result is None
    assert provider.calls == []


# Kills: requiring all locks to implement the optional provider capability.
@pytest.mark.parametrize("lock", [InProcessThreadLock(), object()])
def test_incapable_lock_has_no_guard(lock: object) -> None:
    # Given a non-provider, when resolving an acquisition, then no guard exists.
    assert ownership_guard_for(lock, GRAPH, THREAD, TOKEN) is None


# Kills: replacing provider results or forwarding a wrong graph/thread/token.
@pytest.mark.parametrize("enabled", [False, True])
def test_provider_result_and_acquisition_arguments_are_preserved(enabled: bool) -> None:
    # Given either an opted-out provider or one issuing a specific guard.
    expected = ScriptedGuard() if enabled else None
    provider = RecordingProvider(expected)
    # When resolving the guard.
    result = ownership_guard_for(provider, GRAPH, THREAD, TOKEN)
    # Then the exact issued object and arguments are retained.
    assert result is expected
    assert provider.calls == [(GRAPH, THREAD, TOKEN)]


# Kills: trusting arbitrary provider results or omitting actionable type diagnostics.
@pytest.mark.parametrize("junk", [object(), "guard", 42])
def test_provider_rejects_invalid_result(junk: object) -> None:
    # Given a deliberately malformed capability implementation.
    provider = RecordingProvider(junk)
    # When requesting its guard.
    with pytest.raises(TypeError) as caught:
        ownership_guard_for(provider, GRAPH, THREAD, TOKEN)
    # Then the diagnostic identifies both the provider and its bad return type.
    assert "RecordingProvider.ownership_guard()" in str(caught.value)
    assert f"not {type(junk).__name__}" in str(caught.value)
    assert provider.calls == [(GRAPH, THREAD, TOKEN)]


# Kills: swallowing/replacing provider failures or mutating config before issuance.
def test_provider_failure_propagates_without_partial_config() -> None:
    # Given a failure object and a client config requiring stripping.
    error = RuntimeError("provider fault")
    provider = Fault(error)
    config: dict[str, object] = {KEY: "evil", "configurable": {KEY: "evil", "thread_id": THREAD}}
    snapshot = copy.deepcopy(config)
    # When either the primitive or composed boundary requests a guard.
    for operation in (
        lambda: ownership_guard_for(provider, GRAPH, THREAD, TOKEN),
        lambda: guard_run_config(provider, GRAPH, THREAD, TOKEN, config),
    ):
        with pytest.raises(RuntimeError) as caught:
            operation()
        # Then the original error escapes and the caller's config is untouched.
        assert caught.value is error
        assert config == snapshot


# Kills: unnecessary copies or normalization on the no-guard/no-stray fast path.
@pytest.mark.parametrize(
    "config",
    [None, {}, {"configurable": {"thread_id": "t"}}, {"configurable": None}, {"tags": ["a"]}],
)
def test_clean_config_without_guard_preserves_identity(config: dict[str, object] | None) -> None:
    # Given a clean config, when no guard is injected, then the object is unchanged.
    assert config_with_ownership_guard(config, None) is config


# Kills: mutating inputs, dropping unrelated keys, or failing to inject into a fresh mapping.
@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"tags": ["a"]},
        {"configurable": None},
        {"tags": ["a"], "configurable": {"thread_id": "t", "x": 3}},
    ],
)
def test_injection_copies_config_and_preserves_other_values(
    config: dict[str, object] | None,
) -> None:
    # Given a snapshot before injection (None configurable replacement is pinned behavior).
    snapshot = copy.deepcopy(config)
    guard = ScriptedGuard()
    # When adding an ownership guard.
    result = config_with_ownership_guard(config, guard)
    # Then only the private key is added; neither caller mapping is shared or modified.
    assert result is not None and result is not config
    expected = {} if snapshot is None else dict(snapshot)
    nested = expected.get("configurable")
    expected_nested = dict(nested) if isinstance(nested, dict) else {}
    expected["configurable"] = {**expected_nested, KEY: guard}
    assert result == expected
    result_nested = result["configurable"]
    assert isinstance(result_nested, dict)
    assert result_nested[KEY] is guard
    if config is not None:
        assert result_nested is not config.get("configurable")
    result_nested["result_only"] = True
    assert config == snapshot


# Kills: retaining client keys at either level, adding unwanted configurable, or accumulating data.
@pytest.mark.parametrize(
    "config",
    [
        {KEY: "evil", "tags": ["a"]},
        {"configurable": {KEY: "evil", "thread_id": "t"}, "tags": ["a"]},
        {KEY: "evil", "configurable": {KEY: "evil", "thread_id": "t"}, "tags": ["a"]},
        {KEY: "evil", "configurable": None, "tags": ["a"]},
    ],
)
@pytest.mark.parametrize("inject", [False, True])
def test_client_guard_is_stripped_or_replaced(config: dict[str, object], inject: bool) -> None:
    # Given client-controlled guard values and an optional authentic guard.
    snapshot = copy.deepcopy(config)
    guard = ScriptedGuard() if inject else None
    # When cleaning the same input twice and cleaning the result once more.
    result = config_with_ownership_guard(config, guard)
    repeated = config_with_ownership_guard(config, guard)
    # Then neither stray location survives and all unrelated values remain.
    expected: dict[str, object] = {"tags": ["a"]}
    original_nested = snapshot.get("configurable")
    if isinstance(original_nested, dict):
        expected["configurable"] = {"thread_id": "t"}
    elif "configurable" in snapshot:
        expected["configurable"] = None
    if inject:
        expected["configurable"] = {
            **({"thread_id": "t"} if isinstance(original_nested, dict) else {}),
            KEY: guard,
        }
    assert result is not config
    assert result == expected == repeated
    assert config_with_ownership_guard(result, guard) == expected
    assert KEY not in result
    assert "evil" not in repr(result)
    assert config == snapshot
    if not inject and "configurable" not in config:
        assert "configurable" not in result
    if isinstance(original_nested, dict):
        assert result["configurable"] is not config["configurable"]


# Kills: failing to compose issuance/injection, copying unguarded configs, or skipping sanitization.
@pytest.mark.parametrize("mode", ["capable", "incapable", "no-token", "no-thread", "stray", "none"])
def test_run_config_composes_guard_issuance_and_cleaning(mode: str) -> None:
    # Given the acquisition variants seen by adapters.
    lock = GuardableLock()
    config: dict[str, object] | None = None if mode == "none" else {"tags": ["a"]}
    if mode == "stray":
        config = {KEY: "evil", "configurable": {KEY: "evil", "thread_id": THREAD}}
    backend = InProcessThreadLock() if mode in ("incapable", "stray") else lock
    token = None if mode == "no-token" else TOKEN
    thread_id = None if mode == "no-thread" else THREAD
    # When preparing the adapter's run config.
    result, guard = guard_run_config(backend, GRAPH, thread_id, token, config)
    # Then capable acquisitions issue exactly one guard; all others retain fast-path identity.
    if mode in ("capable", "none"):
        assert len(lock.guards) == 1
        assert guard is lock.guards[0]
        assert lock.issued == [(GRAPH, THREAD, TOKEN)]
        assert result == {**({} if config is None else config), "configurable": {KEY: guard}}
        assert result is not config
    else:
        assert guard is None
        assert lock.issued == []
        if mode == "stray":
            assert result == {"configurable": {"thread_id": THREAD}}
            assert result is not config
            assert "evil" not in repr(result)
        else:
            assert result is config


# Kills: invoking a missing guard or calling a real guard more/less than once.
def test_check_is_noop_for_none_and_calls_guard_once() -> None:
    # Given a real guard that records calls.
    guard = ScriptedGuard()
    # Widen only the return type to observe None without mypy's func-returns-value error.
    check = cast(Callable[[OwnershipGuard | None], object], check_ownership_guard)
    # When checking both absent and present guards.
    absent = check(None)
    present = check(guard)
    # Then both return None and the real guard is checked exactly once.
    assert absent is None and present is None
    assert guard.checks == 1


# Kills: swallowing or wrapping ownership loss or unrelated guard exceptions.
@pytest.mark.parametrize("error", [ThreadLeaseLostError(reason="lost"), ValueError("other fault")])
def test_check_propagates_exact_exception(error: Exception) -> None:
    # Given a guard reporting a specific error object.
    guard = Fault(error)
    # When checking it, then the same object escapes unchanged.
    with pytest.raises(type(error)) as caught:
        check_ownership_guard(guard)
    assert caught.value is error


# Kills: honoring top-level client guards or treating malformed config containers as guards.
def test_resolve_ignores_absent_malformed_and_top_level_guards() -> None:
    # Given absent/non-mapping configurable values, including a misplaced genuine guard.
    configs: tuple[dict[str, object] | None, ...] = (
        None,
        {},
        {"configurable": None},
        {"configurable": "x"},
        {KEY: ScriptedGuard()},
    )
    # When resolving each shape, then no guard is trusted.
    for config in configs:
        assert resolve_ownership_guard(config) is None


# Kills: trusting junk payloads, or discarding a genuine structural guard.
def test_resolve_accepts_only_structural_guards_under_configurable() -> None:
    # Given a real guard amongst representative untrusted values (object has no check).
    guard = ScriptedGuard()
    values: tuple[object, ...] = ("evil", {}, [], 42, True, None, object(), InProcessThreadLock())
    # When resolving the private configurable entry.
    resolved = resolve_ownership_guard({"configurable": {KEY: guard}})
    # Then genuine guards retain identity; every junk value is ignored.
    assert resolved is guard
    for value in values:
        assert resolve_ownership_guard({"configurable": {KEY: value}}) is None


# Kills: failing to reset context on Exception, KeyboardInterrupt, or asyncio cancellation.
@pytest.mark.parametrize(
    "error", [RuntimeError("body fault"), KeyboardInterrupt(), asyncio.CancelledError()]
)
def test_binding_restores_context_after_abnormal_exit(error: BaseException) -> None:
    # Given an initially unbound context.
    guard = ScriptedGuard()
    assert resolve_ownership_guard(None) is None
    # When a bound body exits abnormally.
    with pytest.raises(type(error)) as caught, bind_ownership_guard(guard):
        assert resolve_ownership_guard(None) is guard
        raise error
    # Then the exact failure escapes and context is restored.
    assert caught.value is error
    assert resolve_ownership_guard(None) is None


# Kills: wrong precedence, None masking an outer scope, or stale nested reset tokens.
def test_nested_bindings_restore_outer_guard_and_then_default() -> None:
    # Given distinct scoped/config guards, plus repeated binding of the same object.
    outer, inner, carried = ScriptedGuard(), ScriptedGuard(), ScriptedGuard()
    config = {"configurable": {KEY: carried}}
    # When nesting scoped bindings.
    with bind_ownership_guard(outer):
        assert resolve_ownership_guard(None) is outer
        assert resolve_ownership_guard(config) is outer
        with bind_ownership_guard(None), bind_ownership_guard(outer):
            assert resolve_ownership_guard(None) is outer
        assert resolve_ownership_guard(None) is outer
        with bind_ownership_guard(inner):
            assert resolve_ownership_guard(config) is inner
        assert resolve_ownership_guard(None) is outer
    # Then normal exit restores the default/config fallback, including None-only binding.
    assert resolve_ownership_guard(None) is None
    with bind_ownership_guard(None):
        assert resolve_ownership_guard(config) is carried
    assert resolve_ownership_guard(None) is None


# Kills: process-global binding or losing asyncio.to_thread's copied context.
async def test_binding_thread_propagation_matches_asyncio_contracts() -> None:
    # Given a guard bound in this task and a queue for plain-thread observations.
    guard = ScriptedGuard()
    observations: Queue[OwnershipGuard | None] = Queue()

    def observe_plain_thread() -> None:
        observations.put(resolve_ownership_guard(None))

    # When reading from a plain thread, a copied-context thread, and an executor worker.
    with bind_ownership_guard(guard):
        thread = threading.Thread(target=observe_plain_thread)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        plain = observations.get(timeout=5)
        copied = await asyncio.wait_for(asyncio.to_thread(resolve_ownership_guard, None), timeout=5)
        executor = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, resolve_ownership_guard, None),
            timeout=5,
        )
        # Then only to_thread copies context; neither plain Thread nor executor does.
        assert plain is None
        assert copied is guard
        assert executor is None
        assert resolve_ownership_guard(None) is guard
    assert resolve_ownership_guard(None) is None


# Kills: sharing bindings across concurrently running tasks or resetting another task's token.
async def test_concurrent_tasks_keep_independent_bindings() -> None:
    # Given two tasks coordinated so both bindings are live before either is observed.
    first, second = ScriptedGuard(), ScriptedGuard()
    first_ready, second_ready = asyncio.Event(), asyncio.Event()

    async def observe(
        guard: OwnershipGuard,
        ready: asyncio.Event,
        peer_ready: asyncio.Event,
    ) -> OwnershipGuard | None:
        with bind_ownership_guard(guard):
            ready.set()
            await asyncio.wait_for(peer_ready.wait(), timeout=5)
            result = resolve_ownership_guard(None)
        assert resolve_ownership_guard(None) is None
        return result

    # When both tasks overlap their scoped bindings.
    async with asyncio.TaskGroup() as group:
        one = group.create_task(observe(first, first_ready, second_ready))
        two = group.create_task(observe(second, second_ready, first_ready))
    # Then each task sees only its own guard and the parent remains unbound.
    assert one.result() is first
    assert two.result() is second
    assert resolve_ownership_guard(None) is None
