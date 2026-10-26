# Rolling 0.x hardening status

This is the repository-local disposition for #702, not a new release promise.
Release Please owns versions and release notes; its open PR is not authorization
to publish. Required CI, conversation resolution and release certification remain
unchanged.

## Completed safety work

| Work | Merged evidence |
| --- | --- |
| Native RUN authorization (#687) | #722 |
| Durable JSON result boundary (#688) | #727 |
| Table property sizing / release failures (#689) | #728 |
| Pre-execution cleanup (#690) | #723 |
| Owner-fenced Table release retries (#691) | #725 |
| Streaming terminal observer/cancellation (#692) | #732 |
| Atomic duplicate in-process release (#693) | #724 |
| Bounded Service Bus contention (#694) | #726 |
| Structured SSE events (#695) | #731 |
| Invoke serialization before completion (#696) | #730 |
| Blob renewal safety evaluation (#697) | #733; renewal retained, not replaced with an unsafe fixed deadline |
| Core versus distributed guidance (#699) | #734 |
| Concise multilingual onboarding / Beta positioning (#700) | #745 |
| Native configuration admission (#738) | #740; breaking migration documented |
| Blob task-path replay ordering (#737) | #741 |
| Scale-out thread-store guard (#739) | #742; breaking migration documented |

The `ThreadLock` protocol and shared graph/thread key exclusion are retained.
Local fake-backend tests do not prove all cloud backends or concurrent hosts;
release Azure certification remains a separate gate.

## Remaining decisions

- #701 retains unresolved lifecycle candidates; the decoder-boundary correction
  is tracked by #748. Do not call triage complete until reproductions and focused
  fixes address the remaining cases.
- #698 has concrete session/thread-routing acceptance criteria but does not
  justify silently removing mixed-surface locks. Until implemented, sessions use
  explicit binding kwargs and application-owned routing; keep distributed locks.
- #382 requires owner-operated credential scope/cutover/revocation evidence.
- #371 is external outreach and must not block correctness or releases.

The inexpensive Blob backend remains the first named distributed option. No new
backend or feature family is approved merely by this status document. #702 stays
open while its unresolved children remain; #410 continues the demand-based
expansion checkpoint without adding a v1.0 deadline.
