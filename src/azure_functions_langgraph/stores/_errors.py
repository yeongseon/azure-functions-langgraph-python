class RunLockReleaseError(RuntimeError):
    """An owner-fenced release exhausted its bounded compare-and-swap attempts."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"Run-lock release exhausted CAS attempts for thread {thread_id}")
