# Blob checkpoint storage format and compatibility

This page records what `AzureBlobCheckpointSaver` persists, which parts of that data belong to this
adapter and which to LangGraph, and what is and is not verified when you roll a new release over
existing data. It is derived from the saver source and from fixtures written by real published
releases (see [Evidence](#evidence)). Nothing here is a claim about real Azure Blob Storage: the
fixtures and tests use in-memory containers.

!!! note "Scope"
    The saver's on-blob format is **unchanged**; the only runtime change since this page was introduced is
    that writers refuse to replace a hint of an unknown version (see Known gaps). No migration
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
| Pending write | `.../checkpoints/{checkpoint_id}/writes/{task_id}/{index}.bin` | `serde_type`, `channel`, `task_id`, `task_path` (all percent-encoded) | `serde.dumps_typed(value)` | `put_writes` / `aput_writes` | Path, metadata keys, index rule: adapter. `index` is LangGraph's `WRITES_IDX_MAP` for special channels (negative) or the position in the write batch. A non-negative index is written once and never overwritten; special channels are overwritten |
| Latest hint | `threads/{thread}/ns/{ns}/latest.json` | none | JSON, see below | `put` / `aput` | **Adapter only** |

`ns` is the LangGraph `checkpoint_ns` (subgraph namespaces such as `sub|child:1` are separate
namespaces with their own `checkpoints/`, `values/` and `latest.json`).

### The `latest.json` hint (the only adapter-versioned object)

| Shape | Written by | Meaning |
| --- | --- | --- |
| `{"checkpoint_id": "<id>"}` (no `version`) | 0.8.2 through 0.12.2 (published) | Legacy hint. Current readers **never trust it**; they treat it exactly like a missing hint and scan `checkpoints/` |
| `{"version":1,"dirty":true}` | current writer, first upload of every `put` / `aput` | Intent marker. Not a certification of anything; readers scan |
| `{"version":1,"dirty":false,"checkpoint_id":"<id>"}` | current writer, last upload of every `put` / `aput` | Clean hint. `checkpoint_id` is the greater of the previous hint and the new checkpoint id |

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

## Compatibility statement

Two directions are different questions and are answered separately.

### Current release reads data written by older releases (backward read)

**Verified**, for the writers and scope below: data written by the published releases
`0.8.2`, `0.9.4` and `0.12.2` is read correctly by the current saver through the synchronous API, the
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

* A pre-current writer overwrites `latest.json` with the legacy shape, so the version-1 hint is lost
  (harmless: readers scan).
* If an old writer **commits a checkpoint and dies before rewriting `latest.json`**, a version-1
  clean hint written earlier by a current writer keeps pointing at the previous checkpoint and
  `get_tuple` without a checkpoint id keeps returning it. The newer checkpoint exists and is visible
  through `list` and by id. This is a known gap, pinned by a strict `xfail` test; drain old writers
  for a thread before a current writer touches it.
* A hint written in an unknown **newer** version makes the current writer refuse (see Known gaps).

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
* **Concurrent writers.** Two savers writing one thread at once, without the thread lock, can leave a
  clean hint that points at the lower of two committed checkpoints (the writer that publishes last
  computes `max(hint it read before the other commit, its own id)`). Both checkpoints are committed
  and visible through `list` and by id. Pinned by a strict `xfail`; the thread lock (and, opt-in, the
  ownership guard) is the protection, not the saver.

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
3. **Rolling forward** needs no data step for releases covered by the fixtures: the current reader
   reads existing data and the first new write per namespace publishes the version-1 hint.
4. **Rolling back code** after the new release has written data: older releases are only *probed* for
   reading it (see above). If you must be certain, restore the pre-upgrade backup instead of running the
   older release on data the newer one touched. Writes made after the backup are lost by that choice.
5. `latest.json` is a hint; the saver treats a missing one as "scan". Deleting it forces a scan but also
   makes `collect_orphaned_values` skip the namespace until the next write. Do not hand-edit other
   blobs.

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
