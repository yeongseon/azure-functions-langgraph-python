"""Bounded execution for application readiness callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from inspect import isawaitable
from threading import Lock
from typing import Final, Literal

ComponentStatus = Literal["ok", "failed"]
ReadinessCheck = Callable[[], bool | Awaitable[bool]]

_MAX_WORKERS: Final = 4


async def _await_result(result: Awaitable[bool]) -> bool:
    return await result


def _run_check(check: ReadinessCheck) -> bool:
    result = check()
    if isawaitable(result):
        return asyncio.run(_await_result(result))
    return result


class ReadinessRunner:
    """Run checks with fixed worker and per-component in-flight bounds.

    The mutable future registry is the runner's synchronization state. A timed-out
    check remains registered until it finishes, so later probes reuse it instead
    of submitting more work for the same component.
    """

    def __init__(self, checks: Mapping[str, ReadinessCheck]) -> None:
        self._checks = dict(checks)
        self._executor = ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(self._checks)),
            thread_name_prefix="aflg-readiness",
        )
        self._futures: dict[str, Future[bool]] = {}
        self._lock = Lock()

    def run(self, timeout_seconds: float) -> dict[str, ComponentStatus]:
        """Return sanitized component states within one total probe deadline."""
        futures = self._current_futures()
        done, _ = wait(futures.values(), timeout=timeout_seconds)
        components: dict[str, ComponentStatus] = {}
        for name, future in futures.items():
            if future not in done:
                components[name] = "failed"
                continue
            try:
                components[name] = "ok" if future.result() is True else "failed"
            except Exception:  # noqa: BLE001
                components[name] = "failed"
        return components

    def _current_futures(self) -> dict[str, Future[bool]]:
        with self._lock:
            for name, check in self._checks.items():
                current = self._futures.get(name)
                if current is None or current.done():
                    self._futures[name] = self._executor.submit(_run_check, check)
            return dict(self._futures)
