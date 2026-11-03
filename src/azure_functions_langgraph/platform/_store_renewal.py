from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Final

from azure_functions_langgraph.platform._common import PlatformRouteDeps, logger

RENEWAL_INTERVAL: Final = 20
# Upper bound (seconds) a request cleanup waits for a renewal worker that is
# blocked inside a store call. The worker is a daemon and exits on its own once
# that call returns; it is never force-stopped.
RENEWAL_STOP_TIMEOUT: Final = 5.0


@dataclass(slots=True)
class StoreRenewal:
    """Mutable handoff of a renewal worker's terminal failure to run cleanup."""

    stop: threading.Event = field(default_factory=threading.Event)
    worker: threading.Thread | None = None
    failure: Exception | None = None


class RunLockRenewalError(RuntimeError):
    """The store no longer guarantees ownership of the active run."""


class RunLockRenewalStopTimeoutError(RunLockRenewalError):
    """The renewal worker was still inside a store call when cleanup stopped waiting."""


_STORE_RENEWALS: dict[str, StoreRenewal] = {}


def _start_store_renewal(deps: PlatformRouteDeps, thread_id: str, owner_token: str) -> None:
    renew = getattr(deps.thread_store, "renew_run_lock", None)
    if not callable(renew):
        return
    renewal = StoreRenewal()

    def worker() -> None:
        try:
            while not renewal.stop.wait(RENEWAL_INTERVAL):
                if not renew(thread_id, owner_token):
                    renewal.failure = RunLockRenewalError()
                    logger.error("Run-lock renewal lost ownership for thread %s", thread_id)
                    return
        except Exception as exc:
            # Worker boundary: preserve arbitrary store failures for request cleanup.
            renewal.failure = exc
            logger.error(
                "Run-lock renewal failed for thread %s (%s)", thread_id, type(exc).__name__
            )

    renewal.worker = threading.Thread(target=worker, daemon=True)
    _STORE_RENEWALS[owner_token] = renewal
    try:
        renewal.worker.start()
    except RuntimeError:
        _STORE_RENEWALS.pop(owner_token)
        raise


def stop_store_renewal(owner_token: str) -> Exception | None:
    renewal = _STORE_RENEWALS.pop(owner_token, None)
    if renewal is None:
        return None
    renewal.stop.set()
    worker = renewal.worker
    if worker is not None:
        worker.join(RENEWAL_STOP_TIMEOUT)
        if worker.is_alive() and renewal.failure is None:
            # The in-flight renew cannot be cancelled. Report a terminal state so
            # cleanup records an error; the later store release stays fenced by the
            # owner token and ETag, so a late renew cannot mutate a newer owner.
            logger.error(
                "Run-lock renewal worker still blocked after %.1fs; abandoning it",
                RENEWAL_STOP_TIMEOUT,
            )
            return RunLockRenewalStopTimeoutError()
    return renewal.failure
