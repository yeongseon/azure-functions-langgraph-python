# Blob checkpoint fencing evaluation (proposal)

**Status: proposal; maintainer decision pending.** Evaluated on 2026-11-28
against `origin/main` at
[`5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943`](https://github.com/yeongseon/azure-functions-langgraph-python/tree/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943).
This is evidence and a recommendation for
[#989](https://github.com/yeongseon/azure-functions-langgraph-python/issues/989),
not an accepted ADR, new support promise, implementation, or Azure certification.
Library defaults, stored format, the reference deployment and the runbook are
unchanged. A design/support decision belongs to the maintainer.

## Question and proposed outcome

Can the current marker lease plus Blob saver reject checkpoint commits from an
owner whose lease has expired or been broken, including before local detection?
**No.** The ownership check and data upload are separate operations, and the
service lease applies to a different blob. Conditional hint publication does not
change that boundary.

The recommendation from the issue's
[2026-11-25 evaluation comment](https://github.com/yeongseon/azure-functions-langgraph-python/issues/989#issuecomment-7167231740)
is carried forward **for approval**, not adopted here: explicitly classify the
current combination as unsuitable for strict checkpoint fencing, retain the
opt-in guard and recovery guidance for applications accepting residual risk,
and defer a redesigned commit authority until its protocol and migration are
reviewed. Strict workloads would need a verified ownership-aware commit protocol
or operational prevention of takeover until all old writers have stopped.
Neither alternative is certified by this evaluation.

“Strict fencing” here means storage rejects a stale owner's **logical commit**
at an explicitly defined authority boundary. Merely rejecting a later hint,
tagging data with an epoch, or stopping after detection is insufficient. Even a
future logical-commit fence could allow unreferenced staging bytes to upload.
No checkpoint guarantee includes graph/tool/API side effects or exactly-once
execution; rejection and retries cannot undo an external effect.

## Current implementation evidence

Source links are pinned to the evaluated commit. Tests named here describe
their assertions, not independent-process or live-service certification.

| Mechanism | Code/test evidence | Consequence |
| --- | --- | --- |
| Local guard bound to the original acquisition token | [Blob ownership guard](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/locks/azure_blob.py#L179-L227) | No storage request for a guard check; detected loss raises `ThreadLeaseLostError`. |
| Invocation binds the guard after acquisition and checks again before returning success | [Native handler](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/_handlers.py#L342-L376), [config/binding helpers](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/locks/guard.py#L215-L334) | A failed run can have already uploaded data or performed application side effects. |
| Sync and native async commit: dirty hint → values → metadata → `checkpoint.bin` → clean hint | [`put`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/azure_blob.py#L480-L593), [`aput`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/azure_blob.py#L1489-L1592) | Guard checks precede uploads, not an atomic service-side ownership-and-commit operation. Async executor fallback uses the sync implementation. |
| Ordinary data blobs overwrite; hint writes alone use ETag conditions | [Sync transport](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/_blob_storage.py#L89-L126), [async transport](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/_blob_storage.py#L188-L221) | A stale writer can overwrite an existing checkpoint ID or create a fresh committed ID independently of the marker lease. |
| Ordinary pending-write slots are create-only; mapped special slots overwrite | [`put_writes`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/azure_blob.py#L595-L633), [`aput_writes`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/azure_blob.py#L1594-L1637) | Create-only deduplicates a slot, not a thread owner; stale writers can win previously absent slots. |
| Reads discover `checkpoint.bin` through hints, scan/list or explicit ID | [Format roles and commit/crash states](blob-checkpoint-format.md) | Denying a clean hint cannot retract an already discoverable checkpoint. |
| Retention/deletion are outside the write guard | [Maintenance helpers](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/src/azure_functions_langgraph/checkpointers/azure_blob.py#L635-L751) | Values/hints have separate cleanup behavior; a future fence must account for maintenance and immutable dependencies. |
| Fake two-host characterization retains same-ID overwrite and stale pending-write cases | [Lease-loss characterization](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/tests/test_lease_loss_characterization.py#L118-L270) | Hint conflict detection has not made checkpoint payload overwrites owner-conditional. |

### Residual schedules

These are deductions from the operation ordering, not new Azure measurements:

1. **Pre-detection:** A holds the marker lease, then pauses. The lease expires or
   is broken and B acquires it. A resumes before its renewal loop records loss;
   its local guard passes and its data upload can commit.
2. **Check/upload:** A passes a guard check. Ownership is lost before or during
   the subsequent upload. The upload carries no condition tying it to the marker
   owner. A later failing check does not roll back that upload.
3. **Guard disabled:** A can write even after loss is detected. Requiring guards
   rejects missing-guard calls, but cannot eliminate schedules 1 or 2.

Nominal renewal cadence is not a bound on a frozen host's detection delay. See
[DESIGN §15](https://github.com/yeongseon/azure-functions-langgraph-python/blob/5b0f3885a20e2aaecd3a4e7f70bf5319ae3db943/DESIGN.md#L316-L365)
and the [existing guard contract](production-guide.md#opt-in-guard-for-blob-checkpoint-writes-790).

### What merged hint work changes, and what it does not

[PR #1037](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1037)
merged as `699109fd36f95e12944d7784b444bf9d7a5c2016`. The current tree reads the
hint body and ETag together, publishes dirty intent with If-Match (create-only if
absent), and uses the dirty write's returned ETag for clean publication. An
initial conflict precedes checkpoint uploads. A final conflict follows the
checkpoint commit; fresh-read/conditional dirty invalidation is attempted and
the call still fails. Cleanup can also fail or race. This is overlap detection,
not rollback or fencing.

The normal serialized recovery of a dirty hint scans for the maximum committed
ID and restores the fast path. In contrast, if B publishes a lower clean hint
while A is paused, then A commits a higher ID and dies or cannot invalidate the
hint, a stale **clean** hint may remain. Restoring shared exclusion alone does
not repair it. Stop and drain overlapping writers, then inspect list/explicit
IDs; recovery after failed post-commit invalidation requires incident-specific
validation before resuming. The
[offline legacy recovery procedure](blob-checkpoint-format.md#offline-recovery-of-a-stale-clean-hint)
does not establish concurrent-writer repair, even after they stop.

PR #1037 reports zero added normal-path requests; detected post-commit conflict
adds one GET and one attempted conditional PUT. Those are its recorded
counting-fake/SDK-emulator results, not latency or Azure cost measurements from
this evaluation. Its strict concurrent-success expected failure remains visible.
[#990](https://github.com/yeongseon/azure-functions-langgraph-python/issues/990)
is separate hint-publication work claimed by external contributor **maria2469**;
this proposal neither implements that work nor changes its acceptance status.

## Azure conditional-request semantics

Official sources were retrieved from Microsoft Learn on 2026-11-28. Context7
was attempted but its monthly quota was exhausted; the REST documentation below
is the source of service claims.

| Service contract | Implication for this layout |
| --- | --- |
| [Lease Blob: request, response headers and remarks](https://learn.microsoft.com/en-us/rest/api/storageservices/lease-blob) protects write/delete access to the **addressed blob**. Lease operations do not change its ETag. | Leasing `thread-locks/<scope>/<thread>` does not lease checkpoint/value/pending-write blobs. A marker ETag is not a takeover epoch. |
| [Conditional headers](https://learn.microsoft.com/en-us/rest/api/storageservices/specifying-conditional-headers-for-blob-service-operations) defines If-Match on the resource's ETag; If-None-Match `*` permits only an absent resource. Unmet write preconditions return 412. | ETag CAS protects a particular target version. A fresh stale checkpoint ID can still satisfy create-only; hint CAS cannot condition a separate checkpoint upload. |
| [Put Blob: remarks](https://learn.microsoft.com/en-us/rest/api/storageservices/put-blob#remarks) requires the target's valid active lease ID; a lease ID on a target without an active lease returns 412, including nonexistent targets for REST 2013-08-15+. | Passing the marker's lease ID to another blob does not extend the marker lease. A per-data-blob lease requires its own lifecycle and does not create atomic handoff across blobs. |
| [Conditional-header operation matrix](https://learn.microsoft.com/en-us/rest/api/storageservices/specifying-conditional-headers-for-blob-service-operations#operations-supporting-conditional-headers) supports ETag conditions on Put Blob/Put Block List, not Put Block. Lease Blob's remarks require lease IDs for both block operations on a leased target. | SDK staged-block uploads and retries need explicit investigation; success/failure of one SDK call must not be assumed to cover every service operation atomically. |
| [Blob Batch: remarks](https://learn.microsoft.com/en-us/rest/api/storageservices/blob-batch#remarks) supports tier/delete only; subrequests are independent, non-atomic and unordered. | Batch is not an ownership-plus-checkpoint transaction or atomic retention protocol. |

### Why the obvious retrofits fall short

Reading the marker immediately before upload only moves the check/upload window.
Conditioning each existing data blob on its own ETag protects versions, not the
marker's owner. Same-ID CAS can detect some overwrite races, but cannot reject a
stale writer's fresh ID. An epoch stored in metadata helps diagnosis only unless
**every commit and reader** enforces membership through a fenced authority.
Conditions on `latest.json` alone leave by-ID/list reads of `checkpoint.bin`
unchanged. No combination of these target-local checks is a multi-blob atomic
ownership validation for the current publication protocol.

## Alternatives, costs and migration

These are design alternatives, not selected implementations. Request/cost
estimates below are structural expectations unless explicitly attributed above;
no monetary cost or cloud latency comparison was measured here.

| Alternative | Can stale ownership still commit? | Cost, compatibility, migration and failure semantics |
| --- | --- | --- |
| Current opt-in guard + containment/recovery | **Yes**, before detection and between check/upload; disabled guard also after detection. | Local checks add no storage I/O; existing lock renewal and saver traffic remain. No format/default migration. Operational drain/reconciliation cost; failures can follow partial or complete commit. |
| Enable/require guards by default | **Yes**, same residual windows. | No data migration, but existing success/error/retry behavior changes. Audit custom savers, direct/background calls and all transports; rollback both switches deliberately. Not a strict-fencing solution. |
| ETag/create-only or leases on current data blobs | **Yes**, without a new common authority. | Reads, preconditions, per-blob acquire/renew/release and conflict handling can add requests. Fresh IDs and independent handoffs remain unsafe; rejecting late operations leaves partial data. Payload compatibility alone is not protocol compatibility. |
| Fenced publication authority with immutable staging and committed membership | **Potentially no stale logical commit**, if takeover and publication are enforced on the same authority; staging bytes can still upload. | Requires acquisition-bound identity, a reviewed atomic authority operation, immutable dependencies, all readers honoring membership (including list/by-ID), pending-write/GC/deletion enforcement, legacy reader/writer isolation, rollback and import rules. Authority conflicts reject publication; lost responses require membership reconciliation. Merely adding an epoch or fencing the existing hint is insufficient. |
| Ownership-aware Table EGT or Cosmos transaction | **No after authoritative takeover**, only if ownership validation/update and commit share the transaction and all writers retain their acquisition identity. Generic backend switching: **yes**. | New saver/schema/import/RBAC/recovery design. Table: same table/partition, at most 100 entities and 4 MiB. Cosmos: same logical partition/container, 100 operations, 2 MB and 5 seconds. Conflicts roll back the transaction; ambiguous responses require reconciliation. Partitioning, entity/item capacity and request/RU/storage costs need measurement. |
| Proposed support boundary: no strict fencing for current combination | **Yes**; the risk is disclosed, not eliminated. | Lowest engineering/compatibility cost; no default/format change. Requires explicit workload acceptance and containment. Future fencing is deferred, not rejected. Maintainer approval is still required. |

Transactional service limits and atomicity come from
[Table entity group transactions](https://learn.microsoft.com/en-us/rest/api/storageservices/performing-entity-group-transactions)
and [Cosmos transactional batch](https://learn.microsoft.com/en-us/azure/cosmos-db/transactional-batch).
A Table design would need a conditional ownership update within the change set,
not a separate query (queries cannot accompany writes in it). A Cosmos design
likewise needs conditional ownership enforcement within the transaction. Neither
service retroactively makes a separate Blob marker lease authoritative. Advancing
an epoch on takeover fences that takeover boundary, not necessarily the earlier
instant of nominal lease expiry; the chosen invariant must specify both.

## Evidence limits and acceptance audit

The [recorded real-Azure follow-up](testing.md#real-azure-fault-follow-up-2026-11-24-860-still-partial)
is historical evidence, not a run made for this proposal: lease breaks, successor
acquisition and approximately five-second local detection were observed. Stale
write attempts were **after detection**: guard-off succeeded and guard-on
rejected. It does not prove guarded pre-detection/check-upload races on Azure,
the full conflict matrix, or an ownership-aware transactional alternative.
The [Flex reference](production-reference.md) leaves the guard off and is a
separate deployment, not a guarded reference variant.

The issue comment additionally reports an isolated Azurite 3.35.0 synthetic
comparison: **9 passed, 1 failed, zero skipped**, with a focused repeat of the
failure. The failed lease-conditioned PUT to a nonexistent blob succeeded
contrary to the documented 412 at REST `2023-11-03`. These are **comment-reported
emulator results**, not rerun or independently verified here. Do not discard the
assertion or treat emulator acceptance as Azure behavior; dedicated live
confirmation remains necessary.

| Issue acceptance item | Audited result and remaining work |
| --- | --- |
| Compare recovery/guard with storage-level approaches in a dedicated synthetic environment | Code/service comparison and existing evidence are recorded here. **Incomplete:** no new dedicated live comparison; the reported emulator discrepancy and untested guarded windows require separately authorized Azure work. |
| Record selected approach's cost, compatibility, migration and failure semantics, including stale commits and external effects | Alternatives and the comment's recommendation are evaluated here. **Incomplete:** selection/support acceptance belongs to the maintainer; cloud costs and protocol validation are unmeasured. |
| Update reference/runbook to resulting decision | **Pending:** no accepted resulting decision. Existing pages remain unchanged; this proposal is navigable but does not substitute for approved reference/runbook updates. |

All issue checklist items therefore remain unchecked. This deliverable uses
`Refs #989`, not a completion keyword. No source/runtime, release-file, issue
label, Azure resource or contributor-reserved work is changed.

## Follow-up for maintainer approval

1. Record acceptance, revision or rejection of the proposed support boundary on
   the issue. Keep the stored format and defaults unchanged unless a separately
   reviewed decision explicitly authorizes otherwise.
2. If approved, update the reference safety boundary without rewriting its
   historical guard-off evidence; link an explicit guard opt-in recipe. Expand
   recovery to finite expiry/manual break: pause admission/retries, prevent old
   workers/background saves from resuming, identify the storage-scoped marker,
   verify any stranded lease, quiesce retention/deletion, inspect checkpoint
   lineage/values/pending writes and reconcile external effects before
   authorizing one successor. A lease break alone does not stop the old process.
   Preserve the distinction between dirty recovery and stale-clean incidents.
3. Separately authorize a disposable Azure environment with spend/time limits,
   synthetic data, redacted evidence and teardown. Compare guard-off/on with
   marker-vs-target lease conditions, ETag/create-only behavior, independent
   process takeover, both guarded windows, block uploads/retries/lost responses,
   pending writes and maintenance. Include any ownership-aware transactional
   prototype only after design approval. Record SHA, Python/SDK/REST versions,
   schedules, physical uploads versus logical visibility, request counts,
   latency/cost and cleanup; retain failing assertions and unknown outcomes.
4. Repeat an explicitly guarded Flex variant before attributing those results
   to the reference. Keep hint-publication work and its contributor coordination
   separate. No automatic replay is justified by a rejected/ambiguous checkpoint.
