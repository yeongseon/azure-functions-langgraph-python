# Blob checkpoint storage format and compatibility

This page records what `AzureBlobCheckpointSaver` persists, which parts of that data belong to this
adapter and which to LangGraph, and what is and is not verified when you roll a new release over
existing data. It is derived from the saver source and from fixtures written by real published
releases (see [Evidence](#evidence)). Nothing here is a claim about real Azure Blob Storage: the
fixtures and tests use in-memory containers.

!!! note "Scope"
    The saver's on-blob format is **unchanged**. Writers refuse to replace an unknown hint version and
    use ETag preconditions to detect overlapping hint publication (see Known gaps). No migration
    exists, and no stored data is ever rewritten or re-serialized by a read. A format change is
    proposed, not shipped, in [Proposal](#proposal-versioned-format-marker-not-implemented).

## Inventory of persisted objects

All objects live in the container you pass to the saver. Every path segment is
percent-encoded (`urllib.parse.quote(segment, safe="")`), so an empty namespace appears as `ns//`
and a thread id such as `user/42 é` as `user%2F42%20%C3%A9`. Paths are written by the adapter; they
are not LangGraph concepts.

| Object | Path (relative to the container) | Blob metadata | Payload | Written by | Owner of the format |
| --- | --- | --- | --- | --- | --- |
| Checkpoint (**commit marker**) | `threads/{thread}/ns/{ns}/checkpoints/{checkpoint_id}/checkpoint.bin` | `serde_type`; `parent_id` (percent-encoded) when the checkpoint has a parent | `serde.dumps_typed(checkpoint)` of the LangGraph `Checkpoint` **without** `channel_values` | `put` / `aput`, last data blob of a checkpoint | Layout, metadata keys, commit-marker role: adapter. Payload bytes and the checkpoint schema (`v`, `id`, `ts`, `channel_versions`, `versions_seen`, ...): LangGraph |
| Checkpoint metadata | `.../checkpoints/{checkpoint_id}/metadata.bin` | `serde_type` | `serde.dumps_typed(get_checkpoint_metadata(config, metadata))` | `put` / `aput`, before the commit marker | Layout: adapter. Content: LangGraph |
| Channel value | `threads/{thread}/ns/{ns}/values/{channel}/{version}.bin` | `serde_type` | `serde.dumps_typed(value)`, or the adapter's own `serde_type=empty` with an empty body when a channel has a new version but no value | `put` / `aput`, before metadata | Path and `empty` tag: adapter. Payload bytes: LangGraph. `{version}` is the channel version string (below) |
| Pending write | `.../checkpoints/{checkpoint_id}/writes/{task_id}/{index}.bin` | `serde_type`, `channel`, `task_id`, `task_path` (all percent-encoded) | `serde.dumps_typed(value)` | `put_writes` / `aput_writes` | Path, metadata keys, index rule: adapter. `index` is LangGraph's `WRITES_IDX_MAP` for special channels (negative) or the position in the write batch. A non-negative index is created once with a create-only upload (`overwrite=False`, so an existing blob is left untouched even under a race) and never overwritten; special channels are overwritten |
| Latest hint | `threads/{thread}/ns/{ns}/latest.json` | none | JSON, see below | `put` / `aput` | **Adapter only** |

`ns` is the LangGraph `checkpoint_ns` (subgraph namespaces such as `sub|child:1` are separate
namespaces with their own `checkpoints/`, `values/` and `latest.json`).

### The `latest.json` hint (the only adapter-versioned object)

| Shape | Written by | Meaning |
| --- | --- | --- |
| `{"checkpoint_id": "<id>"}` (no `version`) | 0.8.2 through 0.12.2 (published) | Legacy hint. Current readers **never trust it**; they treat it exactly like a missing hint and scan `checkpoints/` |
| `{"version":1,"dirty":true}` | 0.13.0 and current writer, first upload of every `put` / `aput` | Intent marker. Not a certification of anything; readers scan |
| `{"version":1,"dirty":false,"checkpoint_id":"<id>"}` | 0.13.0 and current writer, last upload of every `put` / `aput` | Clean hint. `checkpoint_id` is the greater of the previous hint and the new checkpoint id |

A reader follows the hint only when `version == 1` and `dirty is False` and `checkpoint_id` is a
non-empty string. Anything else (malformed JSON, non-object, other `version`, missing fields) is
ignored for reads and the authoritative prefix scan is used. A hinted checkpoint that cannot be built
also falls back to the scan.

### Channel version strings and `serde_type`

* Channel versions are produced by the adapter's `get_next_version`:
  `f"{n:032}.{r:.16f}"` (a 32-digit counter, a dot, a 16-digit pseudo-random fraction used only for
  uniqueness). Existing versions are parsed through their integer part. The string is part of the
  `values/` path.
* `serde_type` is the tag returned by the configured LangGraph serializer (the default writes
  `msgpack`). The adapter adds exactly one tag of its own, `empty`. Whether a serializer tag or its
  payload can be read by another `langgraph-checkpoint` version is decided by LangGraph, not by this
  adapter.

### What carries no format version

The checkpoint, metadata, value and write blobs carry **no adapter format version**. Their format is
implied by the path layout, the metadata keys listed above, and the serializer tag. That implied
format is identical across every committed fixture set (see
[Evidence](#evidence)): between 0.8.2 and the current writer only `latest.json` changed.

### Not checkpoint data

`AzureBlobLeaseThreadLock` writes separate marker blobs under its own prefix (default
`thread-locks/`) to hold leases; see the [Production Guide](production-guide.md). They are not
checkpoint objects and are outside this format.

## Write order and crash states

A `put` writes, in this order: dirty marker, channel values, metadata, checkpoint (the commit
marker), clean hint. A checkpoint **exists if and only if** its `checkpoint.bin` exists. Consequences
for a crash:

| Crash point | State left | What readers return |
| --- | --- | --- |
| After the dirty marker | `latest.json` dirty | Previous checkpoint (scan) |
| After some values / metadata | Orphan value or metadata blobs; no `checkpoint.bin` | Previous checkpoint. Orphans are harmless; a retry overwrites them. `collect_orphaned_values` can reclaim unreferenced value blobs |
| After `checkpoint.bin`, before the clean hint | `latest.json` dirty | The new checkpoint (scan finds it) |

Pending writes are independent objects: a crash mid-batch leaves a prefix of the batch.

All of the rows above are covered by `tests/test_blob_format_compat.py`.

### Why `put` probes the hinted checkpoint (one HEAD per write)

Before the dirty marker, `put` / `aput` read the clean hint and send one existence request (a HEAD) for
the checkpoint it names. That request looks avoidable but is part of the behavior, not overhead: the hint
the writer publishes last is `max(previous hint, new id)`, so the previous hint must name a checkpoint
that exists. If it does not, the writer scans `checkpoints/` instead (the same path as a missing or dirty
hint).

A clean hint can name a missing checkpoint through the public API: `delete_checkpoints_before` and
`delete_old_checkpoints` never touch `latest.json`. For such a hint, without the probe:

* a hint **above** the id being written would be re-published and keep naming nothing (readers would
  fall back to the scan on every read);
* a hint **below** the id being written, with a newer committed checkpoint present (concurrent writer or
  clock skew between instances), would be replaced by the new, lower id: `get_tuple` without an id would
  stop returning the newest committed checkpoint. The scan that finds it is the probe's consequence.

Skipping the probe only when the hint is below the new id does not help (the second case). Both states
are pinned for the sync, native-async and executor-fallback writers by
`test_put_probes_the_hinted_checkpoint_before_trusting_it` and
`test_put_probe_stops_a_dangling_hint_from_hiding_a_newer_committed_checkpoint`; the request count (1 GET
for the hint, 1 HEAD) is pinned by `tests/test_benchmark_blob_checkpointer.py`. The cost is one small
request per `put` against a blob that is normally present; it is not measured against real Azure latency.

## Compatibility statement

Two directions are different questions and are answered separately.

### Current release reads data written by older releases (backward read)

**Verified**, for the writers and scope below: data written by the published releases
`0.8.2`, `0.9.4`, `0.12.2` and `0.13.0` is read correctly by the current saver through the synchronous API, the
native async API and the thread-executor async fallback. "Correctly" means: latest and by-id
lookup, `list` ordering and parent chain, namespaces, special-character thread ids, channel values
(including `empty` blobs), metadata, and pending writes in replay order. Reads modify nothing, and
the current writer can continue a legacy thread without rewriting any legacy blob (only
`latest.json` changes).

Scope of that evidence: one deterministic scenario, simple values, Python 3.12 writers,
`langgraph 1.2.11` / `langgraph-checkpoint 4.2.0`, in-memory containers. Not shown by it:

* other `langgraph-checkpoint` versions *writing* (serializer compatibility belongs to LangGraph).
  The fixtures, written with `langgraph-checkpoint 4.2.0`, were also read locally under the declared
  floors (`langgraph 1.0.0`, `langgraph-checkpoint 2.1.2`, Python 3.11) and the `Minimum dependencies`
  CI lane runs them there; neither is a statement about versions in between,
* custom serializers, custom `Checkpoint` content, very large threads,
* releases not fixtured: `0.9.0`-`0.9.3`, `0.10.0`, `0.11.4`, `0.12.0`. Their `azure_blob.py` is
  byte-identical to `0.9.4` (0.9.x) or `0.12.2` (0.10.0 onward), but that identity is of one module,
  not of the whole wheel.

Which releases can have written Blob data at all: the PyPI wheels `0.5.x` through `0.8.1` contain no
package code (they list only `*.dist-info` files), so no Blob data written through a package from
those PyPI files exists to be compatible with. `0.8.2` is the first PyPI wheel containing the saver.
The ten tags absent from PyPI (see [Release Status](release-status.md)) cannot be fixtured.

The legacy path is explicit: an unversioned `latest.json` is accepted as "no usable hint" and the
scan is authoritative; a thread keeps working, and the first write by the current saver publishes a
version-1 hint. Until then `collect_orphaned_values` conservatively skips the namespace.

### Older release reads data written by a newer one (downgrade / reverse read)

**Only probed, not guaranteed.** `tests/fixtures/blob_compat/reverse_read_probe.py` was run
by hand with the published `0.8.2`, `0.9.4` and `0.12.2` synchronous readers against the current-writer
fixture set, with the clean hint and with every hint replaced by the dirty marker. In that scenario each
old reader found the newest checkpoint and the expected channel values for every thread and namespace;
**pending writes came back as the same set in a different order**, because those releases order by task
id and index, not by task path. That outcome is not asserted by CI, was not run against the old async
paths or other data, and no old release is promised to read data written by a newer one. A future
format change must be assumed unreadable by older releases unless a probe shows otherwise.

### Mixed fleets (old and new writers on one thread)

Not safe. The adapter assumes a single writer per thread; the blob thread-lock scope also changed
between releases (see the Production Guide's rolling-deployment warning). On top of that:

* A pre-0.13 writer overwrites `latest.json` with the legacy shape, so the version-1 hint is lost
  (harmless: readers scan).
* If an old writer **commits a checkpoint and dies before rewriting `latest.json`**, a version-1
  clean hint written earlier by a current writer keeps pointing at the previous checkpoint and
  `get_tuple` without a checkpoint id keeps returning it. The newer checkpoint exists and is visible
  through `list` and by id. This is a known gap, pinned by a strict `xfail` test; drain old writers
  for a thread before a current writer touches it. For an already affected namespace, use the
  [offline recovery procedure](#offline-recovery-of-a-stale-clean-hint) below.
* A hint written in an unknown **newer** version makes the current writer refuse (see Known gaps).

#### Offline recovery of a stale clean hint

This procedure recovers **visibility of existing committed data**, not a failed run or lost external
side effects. It uses the current persisted layout and existing public saver operations; there is no
new API, format migration or checkpoint rewrite. Use the same container and serializer as the app.

1. **Stop admission and drain all activity for the affected thread**, old and new processes, including
   retries, background Activities, pending-write producers, retention and orphan collection. Hold
   that maintenance boundary through verification. A lease alone is not proof of quiescence: old
   releases can use different lock scopes, and a lost lease does not fence storage writes. Stop if
   you cannot exclude another writer. Back up the whole affected thread prefix before mutation
   (see [Rollback and backup guidance](#rollback-and-backup-guidance)); keep checkpoint contents out
   of incident logs.
2. **Detect, read-only, per namespace.** Supply explicit `thread_id` and `checkpoint_ns` (including
   `""` for the root); omit `checkpoint_id`, `before` and metadata filters. Compare
   `saver.get_tuple(config)` with `next(saver.list(config, limit=1), None)`. `list` ignores the hint
   and enumerates commit markers in descending checkpoint-id order. The precise stale-read signal
   is that the listed tuple's `config["configurable"]["checkpoint_id"]` is lexicographically greater
   than the unqualified read's id, while `latest.json` is a version-1 clean hint naming that older
   id. There is **no automatic warning**, and a clean hint's age or well-formedness is not a detection
   signal. Equal ids mean this check found no stale latest read. No tuple, unexpected hint contents
   or inconsistent results require investigation rather than deletion.
3. **Validate the recovery target.** In the namespace's `checkpoints/` listing, only paths ending in
   `/checkpoint.bin` are commit markers; metadata/value/write-only prefixes are not committed
   checkpoints. The greatest decoded checkpoint id must match the candidate returned by `list`.
   Read it with `saver.get_tuple(candidate.config)` to bypass the hint, and confirm the payload id,
   channel values, metadata, parent and pending writes are the expected recoverable state. Use
   checkpoint-id ordering, not timestamps or blob last-modified time: "latest" here means greatest
   committed id, not necessarily last wall-clock completion. If the greatest marker cannot be read,
   differs from the payload id, or has missing/corrupt dependencies, stop and investigate/restore the
   backup; do not silently select an older readable checkpoint.
4. **Invalidate only the affected namespace's hint.** After confirming it is the known stale
   version-1 clean hint, delete the base blob
   `threads/{quote(thread_id, safe="")}/ns/{quote(checkpoint_ns, safe="")}/latest.json` using the
   authenticated storage client already targeting that container (Python SDK:
   `container.get_blob_client(hint_path).delete_blob()`). Preserve percent encoding, empty namespace
   and the exact container; do not delete a prefix. A missing hint on a repeat run is already
   invalidated (handle only that blob's `BlobNotFound` as success). Authentication, lease, snapshot,
   policy or other errors are **not** success: stop rather than broadening the deletion, removing
   snapshots or changing storage policy. Do not hand-publish a replacement clean hint or call `put`
   just to repair visibility. Checkpoint, metadata, channel-value and pending-write blobs stay intact.
5. **Verify before resuming.** Repeat the unqualified `get_tuple(config)` and by-id read and compare
   their config ids and full checkpoint tuples with the scanned candidate. They must agree. Repeat
   for each affected namespace, including subgraphs; repairing the root does not repair children.
   Reads leave the hint absent and scan on every latest lookup. Resume only a drained, single-writer
   deployment of the chosen current release, under its supported locking configuration. Its next
   normal checkpoint write restores the hint through the existing writer behavior; do not manufacture
   a checkpoint or replay a graph solely to restore the optimization.

**Cost and risk.** `list(..., limit=1)` bounds tuple reconstruction, **not** discovery: it lists the
whole namespace's `checkpoints/` prefix (including metadata and pending-write names), materializes and
sorts the ids, then downloads the candidate's metadata, checkpoint, referenced values and pending
writes. Expect paginated listing cost proportional to history, id-sort memory/CPU, tuple downloads
proportional to state and pending writes, plus the original hinted read, a by-id read and verification
reads. Marker inspection adds another full listing if performed separately. Invalidation adds one
DELETE per affected hint; the backup has its own full-prefix cost. While the hint is absent, every
latest read pays the scan cost and orphan collection conservatively skips that namespace until a
normal current write restores a usable hint. No checkpoint data is lost by hint invalidation, but
resuming from the newly visible state can change application behavior; pending tasks/external side
effects still need application-level idempotency and operator review.

**Safe to re-run only while quiesced.** Detection, by-id inspection and scans are read-only; deleting
an already missing hint changes nothing. Re-running verification returns the same committed tuple.
Once writers resume, start again with draining and a fresh diagnosis, not a saved candidate.
Do **not** use this for live mixed-version operation, concurrent-writer repair, format-version refusal
(never delete an unknown-version hint to bypass the writer guard), data corruption, incomplete
commits, retention recovery, rollback to an older checkpoint, or forced cancellation/fencing.

`tests/test_blob_legacy_recovery.py` verifies detection, by-id recovery and repeated hint invalidation
over the real `0.8.2`, `0.9.4`, `0.12.2`, `0.13.0` and `current` fixture bytes with synthetic stale
version-1 hints, for sync, native-async and executor-fallback readers. It checks full expected state,
namespaces, encoded thread ids and unchanged non-hint blobs. These are in-memory format/adapter tests,
not evidence of Azure authorization, delete policy, service consistency or runtime cancellation.
The original strict expected-failure remains: ordinary reads are not automatically repaired.

#### Why the stale hint is not mitigated on the read path

Detecting it needs a cross-check that no checkpoint newer than the hinted one exists. Without a lower
bound that is a listing of the namespace's whole `checkpoints/` prefix, which is exactly the scan the hint
exists to avoid (Azure lists in ascending name order, so the newest id is only found by enumerating every
checkpoint; the cost grows with history length). A bounded listing would need the `start_from` option of
`ContainerClient.list_blobs`, and that is not safe to rely on here: the package supports
`azure-storage-blob>=12.0,<13`, older versions of which do not have the keyword; the SDK documents it as
"Only one entity level is supported", and its behavior on a flat (non-hierarchical) account cannot be
verified without a real service, which this work does not use. Even if it worked it adds a request to every
`get_tuple`, which conflicts with the Blob I/O reduction tracked in #860, and the "only when needed" variant
has no reliable trigger because the stale hint is a perfectly valid clean hint. The gap only affects a mixed
old/new fleet that is already unsupported. It therefore stays documented and pinned. A format change that
lets a current reader detect an old writer (for example the adapter-owned marker in the Proposal below) is
the way to close it.

## Known gaps

These are current behavior, not guarantees; each is pinned by a strict `xfail` test so a fix flips the
test and forces this page to change.

* **Stale clean hint after an old writer's crash** (see Mixed fleets). Not fixed; see
  [Why the stale hint is not mitigated on the read path](#why-the-stale-hint-is-not-mitigated-on-the-read-path).
* **Concurrent writers.** The supported contract is **one writer per thread, enforced by the
  thread lock**. All instances and direct maintenance writers must share that exclusion scope.
  The saver now detects overlapping hint changes, rather than offering concurrent-write success:
  a stale initial snapshot or final publication raises `BlobCheckpointConflictError` (a
  `RuntimeError`, importable from `azure_functions_langgraph.checkpointers.azure_blob`). The strict
  `xfail` remains visible because two overlapping `put` calls are not promised to succeed.

### Conditional hint publication and recovery

`put` / `aput` read the hint body and its ETag in the same GET. The dirty upload uses
`If-Match` against that snapshot, or create-only `If-None-Match: *` when the hint was absent.
The final clean upload uses `If-Match` against the ETag returned by **that writer's dirty upload**.
ETags are request/response state; no blob name, payload, version or metadata field changes.
The same rules apply to sync, native async and executor-fallback writes.

- An initial conflict is reported before uploading any checkpoint data. There is no automatic retry.
- A final conflict occurs **after the checkpoint commit marker**. The saver attempts to invalidate
  the hint with a fresh GET and a conditional dirty PUT, then raises the conflict. If another change
  races this cleanup, or storage/ownership checks fail, that failure propagates. An unknown hint
  version is preserved. No unconditional cleanup write or blind retry replaces another writer's state.
- With successful invalidation and no further overlapping publication, an unqualified read scans
  and returns the largest committed ID, regardless of commit/publication order. Both distinct
  checkpoints remain directly readable.
- **Failed invalidation or a process exit can still leave a stale clean hint under unsupported
  overlapping writers.** For example, B cleans its lower ID while A is paused; A commits a higher ID
  and dies before invalidation. A hint precondition cannot atomically fence a different checkpoint blob.
  Do not interpret a write error as rollback, or use this detector instead of the thread lock.
- Under the supported single-writer contract, a writer that dies after marking dirty leaves readers
  on the authoritative scan path. **The next lock-holding writer scans, commits, and cleans the hint**,
  including when its own ID sorts below an already committed ID. This restores the fast path without
  operator deletion of the hint. A timeout after the service accepted a clean write is also safe to
  inspect/retry under the same exclusion contract.

On conflict, **stop and drain overlapping writers first**, including retries and maintenance activity,
then inspect committed IDs through `list` and by-ID reads. A dirty hint can be repaired by the next
serialized write under the shared thread lock. **Restoring the lock alone does not repair a stale
clean hint**: subsequent reads and writes can still trust the older committed ID it names.

For stale clean hints left by legacy writers, #991's
[offline recovery procedure](#offline-recovery-of-a-stale-clean-hint) (merged in #1024) documents
quiescent detection, target validation, hint-only invalidation and verification. Its fixture tests
verify visibility recovery for that legacy-writer scenario, not concurrent-writer repair or Azure
service behavior; the procedure explicitly excludes concurrent-writer repair. Recovery after failed
post-commit invalidation by overlapping writers remains **unverified by this PR and #1024**, even
after those writers are stopped. Do not treat the legacy procedure as verified steps for that case,
or resume on the assumption that a blind write retry will repair it; incident-specific investigation
and validation are still required before resuming under shared exclusion.
Lease-loss fencing remains outside this protocol; a stale writer with a newly read ETag can still
write checkpoint data. Pending-write semantics and retention maintenance are unchanged.

**Cost:** the normal clean-hint single-writer path adds **zero requests**: one hint GET, one
checkpoint-existence HEAD and the same dirty/data/clean PUTs. A first write replaces one PUT with
one create-only PUT, without changing the total. A final detected conflict adds a GET and one attempted
conditional dirty PUT; that attempted cleanup can itself fail. Dirty recovery still pays the existing
checkpoint-prefix LIST cost (including pagination); clean latest reads retain their fast path.

Why not automatic multi-writer reconciliation? Post-commit ID comparison/ETag retries or read-back
repair handle successful publishers but cannot close the commit-to-hint crash window. Permanently
retaining dirty after overlap avoids trusting a stale hint but also prevents the next single writer
from automatically restoring the fast path: the unchanged dirty shape cannot distinguish a dead
writer from one that may still commit. This proposal therefore keeps lock-based exclusion, adds
loud detection, and preserves automatic recovery rather than claiming a multi-blob transaction.

Resolved: **a hint of an unknown version is no longer overwritten.** `put` / `aput` raise
`BlobCheckpointFormatError` (a `RuntimeError`, importable from
`azure_functions_langgraph.checkpointers.azure_blob`) before uploading anything when `latest.json` is a JSON object
whose `version` is present but is not the integer `1` (for example `2`, `"1"`, `null`, `true`). The
message names an integer version (or says "unrecognized") and never includes blob content, thread ids or
paths. Reads (`get_tuple`, `list`, async twins) still ignore such a hint and use the prefix scan, and leave it
untouched; `collect_orphaned_values` skips the namespace. Hints that are missing, legacy (no `version`),
dirty, malformed JSON or not a JSON object are still treated as "no usable hint" and replaced as before.
`put_writes` / `aput_writes` do not consult the hint and are unaffected.

!!! warning "Behavior change for downgrade scenarios only"
    A release that does not understand a newer hint version now refuses to write to that namespace instead
    of silently replacing the hint. No behavior changes for data written by this or any earlier release,
    because no published release has written a hint with another version. The refusal can only occur after a
    newer release wrote such a hint and an older one is then run on the same data (a downgrade, which this
    page already treats as unsupported). Recovery: run the newer release again, or restore the pre-upgrade
    backup (see Rollback and backup guidance).

Unknown or malformed *data* blobs already fail loudly: an unknown `serde_type` or invalid payload
raises (`NotImplementedError` / `ValueError` from the serializer) from `get_tuple`, `list` and their
async counterparts, and nothing is modified. A blob with no `serde_type` metadata is treated as
absent.

## Rollback and backup guidance

This adapter never migrates data, so rollback means restoring *storage*, not a code downgrade alone.

1. **Before upgrading** the checkpoint container, enable recoverability on the storage account
   (not provided by this package): [soft delete for blobs][soft-delete],
   [blob versioning][versioning] and, if you need to restore a consistent point in time,
   [point-in-time restore for block blobs][pitr]. Alternatively copy the container (or the `threads/`
   prefix) to a backup container with a tool such as AzCopy. Because a checkpoint is several blobs,
   prefer a point-in-time or whole-prefix copy to restoring single blobs.
2. **Quiesce writers** for the affected threads (drain old and new instances) before the code
   rollout and before any rollback; see Mixed fleets above.
3. **Rolling forward** from the fixtured releases supports backward reads after a drained cutover,
   not overlapping old and new writers on one thread. In particular, drain pre-0.13 writers before
   any 0.13-or-newer writer touches their threads. Ordinary legacy hints need no data step: the
   current reader scans and the first normal new write per namespace publishes a version-1 hint.
   If old writers may have run after newer ones, audit each affected namespace with the
   [offline detection and recovery procedure](#offline-recovery-of-a-stale-clean-hint) before
   resuming; draining alone does not invalidate an already stale clean hint.
4. **Rolling back code** after the new release has written data: older releases are only *probed* for
   reading it (see above). If you must be certain, restore the pre-upgrade backup instead of running the
   older release on data the newer one touched. Writes made after the backup are lost by that choice.
5. A code rollback is not permission to reintroduce a pre-0.13 writer into a newer fleet. Drain both
   generations first and restore a consistent backup for a guaranteed rollback boundary. If a later
   forward cutover reuses data touched by both generations, perform the offline audit again. Hint
   invalidation restores latest visibility only; it neither makes reverse reads supported nor
   converts newer data into an older format. Never remove an unknown-version hint to force a downgrade.

[soft-delete]: https://learn.microsoft.com/en-us/azure/storage/blobs/soft-delete-blob-overview
[versioning]: https://learn.microsoft.com/en-us/azure/storage/blobs/versioning-overview
[pitr]: https://learn.microsoft.com/en-us/azure/storage/blobs/point-in-time-restore-overview

## Evidence

Fixture sets live in `tests/fixtures/blob_compat/sets/<label>/` (`blobs.json`, `expected.json`,
`environment.json`) with provenance and regeneration steps in the README beside them. Past-version sets
were produced by installing the **PyPI wheel** of that exact version (SHA-256 recorded and checked
against PyPI's metadata) into a throwaway Python 3.12 virtual environment and running its real
`AzureBlobCheckpointSaver`, synchronous and (from 0.9.0) native-async, against an in-memory
container. `expected.json` is derived from the scenario's inputs, not from reading the blobs back.

| Set | Source | Hint shape | Async writer |
| --- | --- | --- | --- |
| `0.8.2` | PyPI wheel | legacy | no (predates #440) |
| `0.9.4` | PyPI wheel | legacy | yes |
| `0.12.2` | PyPI wheel | legacy | yes |
| `0.13.0` | PyPI wheel | version 1 | yes |
| `current` | this repository at the commit recorded in `environment.json` | version 1 | yes |

## Compatibility gate for any future format change

A change to anything in the inventory (paths, metadata keys, `serde_type` handling, hint shape,
version strings) is a stored-format change. It must, in the same pull request:

1. add a fixture set produced by a **real writer of the new format** (the generator script is the
   only supported way), and register it in `STORED_HINT_FORMATS` in
   `tests/test_blob_format_compat.py`. `test_every_emitted_format_version_has_fixture_sets` fails
   if the current writer emits a stored hint version that has none;
2. keep every existing fixture set byte-for-byte and keep `test_old_writer_to_current_reader` green
   for all of them (never regenerate or edit a past set);
3. define behavior for unknown versions (reject, do not overwrite; for the hint this is
   `BlobCheckpointFormatError`) and for the legacy path, with tests;
4. record a downgrade probe result in this page;
5. be classified for breaking-change impact per
   [CONTRIBUTING.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CONTRIBUTING.md#breaking-change-classification);
   a release that changes stored data must not be justified by new-version round-trip tests alone.

## Proposal: versioned format marker (not implemented)

Nothing here is required by the current data: the layout has been byte-stable from 0.8.2 and the only
versioned object, the hint, is already self-describing. When a change to the layout *is* needed:

* add one adapter-owned marker, for example an `adapter_format` blob-metadata key written only by
  **new** writers on the blobs they create (absent means legacy format `0`), so existing blobs are
  never rewritten and legacy reads stay explicit;
* readers accept `0` and the versions they know, and fail closed on a greater value;
* writers refuse to write into a namespace whose hint or marker is of an unknown greater version,
  instead of overwriting it (implemented for the `latest.json` hint only, see Known gaps; a new marker
  would need the same guard);
* gate the change with a new fixture set per the rules above.

This needs its own issue and review.
