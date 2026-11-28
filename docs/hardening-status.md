# Rolling 0.x hardening status

This is the repository-local disposition for the completed #702 hardening tracker
(closed 2026-11-06), not a new release promise. Release Please owns versions and
release notes; its open PR is not authorization to publish. Required CI,
conversation resolution and release certification remain unchanged.

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
| Opt-in session-aware Service Bus routing (#698) | #754, #757, #795; live broker proof on Standard tier; retains distributed lock by default |
| Core versus distributed guidance (#699) | #734 |
| Concise multilingual onboarding / Beta positioning (#700) | #745 |
| Deferred runtime edge triage (#701) | #748, #751, #753, #755, #756; triaged with focused fixes; not Azure-certified |
| Native configuration admission (#738) | #740; breaking migration documented |
| Blob task-path replay ordering (#737) | #741 |
| Scale-out thread-store guard (#739) | #742; breaking migration documented |

The `ThreadLock` protocol and shared graph/thread key exclusion are retained.
Local fake-backend tests do not prove all cloud backends or concurrent hosts;
release Azure certification remains a separate gate.

## Remaining decisions (as of 2026-11-24)

- #701 triage is closed (2026-10-27). Reproductions produced focused fixes in
  #748 (decoder recursion), #751 (Durable host cancellation), #753 (stream
  construction), #755 (Cosmos emulator client cleanup), and #756 (transport
  ownership handoff). Other candidates were dispositioned using existing tests
  and evidence. Closing the triage tracker does not assert production Azure
  certification or prove unresolved hypotheses in `docs/deferred-edge-triage.md`
  are fixed in cloud environments.
- #698 is closed (2026-11-05). Implementation shipped in #754 and #757 with an
  explicit `session=True` opt-in and `session_exclusive=True` bypass gate. Live
  broker proof on Azure Service Bus Standard tier in koreacentral is recorded in
  #795 (`docs/production-guide.md`). The live tests showed that session-lock loss
  can cause message overlap across competing hosts, so distributed locking
  (`ThreadLock`) remains required for multi-host and mixed-surface topologies.
- #382 remains open. It requires owner-operated credential scope, cutover, and
  revocation evidence.
- #371 remains open. It is external outreach and does not block correctness or
  releases.
- Currently open follow-ups: #410 continues the demand-based expansion checkpoint
  without adding a v1.0 deadline. Production Azure certification and hosting
  validation are tracked by open issues #880, #882, #883, and #811. Table search
  indexing is tracked by open issue #801.

The inexpensive Blob backend remains the first named distributed option. No new
backend or feature family is approved merely by this status document. Tracker
#702 closed on 2026-11-06 following the completion of child issues #687 through
#701 and verification of the v0.13.0 release gates.
