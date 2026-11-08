class RunLockReleaseError(RuntimeError):
    """An owner-fenced release exhausted its bounded compare-and-swap attempts."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Run-lock release exhausted CAS attempts for thread {thread_id}")


class RunLockRenewalConflictError(RuntimeError):
    """A renewal exhausted its bounded compare-and-swap attempts while still owning the lock.

    Every attempt re-read the row and found the caller's token still owning a busy thread, but
    the conditional write kept losing to concurrent writes. That is neither proof that ownership
    was replaced nor a successful extension, so it is raised instead of being returned as a
    plain ``False``.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Run-lock renewal exhausted CAS attempts for thread {thread_id}")
