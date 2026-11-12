# Blob checkpoint compatibility fixtures (#881)

Blob names, metadata and bytes written by **real** `AzureBlobCheckpointSaver` implementations, used by
`tests/test_blob_format_compat.py` to prove old-writer to new-reader compatibility. The format they
document is described in `docs/blob-checkpoint-format.md`.

## Sets

| Set | Writer | Source artifact | Python | `langgraph` | `langgraph-checkpoint` | `azure-storage-blob` | Sync writer | Async writer | Hint |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `0.8.2` | published release | PyPI wheel, sha256 `69be6102...79cb` | 3.12.16 | 1.2.11 | 4.2.0 | 12.31.1 | yes | no (predates native async, #440) | legacy `{"checkpoint_id"}` |
| `0.9.4` | published release | PyPI wheel, sha256 `3a6e2fe1...2ab0` | 3.12.16 | 1.2.11 | 4.2.0 | 12.31.1 | yes | yes | legacy |
| `0.12.2` | published release | PyPI wheel, sha256 `16c4b754...8faa` | 3.12.16 | 1.2.11 | 4.2.0 | 12.31.1 | yes | yes | legacy |
| `0.13.0` | published release | PyPI wheel, sha256 `4c4c9241...1d91` | 3.12.16 | 1.2.11 | 4.2.0 | 12.31.1 | yes | yes | `{"version":1,"dirty":false,...}` |
| `current` | this repository (`environment.json` has the commit) | source tree, `PYTHONPATH=src` | 3.14.4 | 1.2.11 | 4.2.0 | 12.30.1 | yes | yes | `{"version":1,"dirty":false,...}` |

Full hashes are in each set's `environment.json` (`source_ref`). Each wheel's SHA-256 was compared with
the digest PyPI's JSON API reports for that file.

Why these versions. Of the 20 versions on PyPI, wheels `0.5.x`-`0.8.1` contain no package code (only
`*.dist-info`), so they cannot have written Blob data. Of the 14 versions that do contain the saver,
`0.9.0`-`0.9.4` share one `azure_blob.py` and `0.10.0`-`0.12.2` share another (identical MD5), so one set
per distinct module is committed: first release with the saver (`0.8.2`), the async generation
(`0.9.4`), the latest release before the version-1 hint (`0.12.2`), and the first release that writes the
version-1 hint (`0.13.0`). The `0.13.0` set was produced from the published wheel (it could not be
downloaded in the earlier sessions that produced the other sets because of read timeouts from
`files.pythonhosted.org`; it was later fetched from a PyPI mirror and the bytes were checked against the
sha256 the PyPI JSON API reports for that file, `4c4c924182861a1a0fdb47058a19611d448352be7eadcff7cca95e34aec71d91`). The earlier
run against the `v0.13.0` **tag source** had matched `current` byte for byte; the wheel run does too
(`blobs.json` and `expected.json` differ from `current` only in the `label` field), which is the evidence
that the published artifact and the tag source wrote the same bytes. The `0.13.0` environment is the
`0.12.2` environment with the wheel replaced (same Python 3.12.16, `azure-storage-blob` 12.31.1,
`langgraph` 1.2.11, `langgraph-checkpoint` 4.2.0, `langchain-core` 1.6.2). The ten tags missing
from PyPI (`docs/release-status.md`) cannot be fixtured.

All five sets were written with the same `langgraph`/`langgraph-checkpoint` so that differences between
them can only come from this adapter. Their non-`latest.json` blobs are byte-identical, which
`test_only_the_hint_differs_between_fixture_generations` asserts.

## Files per set

* `blobs.json`: every blob the saver left, sorted by name: `name`, `metadata`, `data_b64`.
* `expected.json`: the logical content derived from the scenario **inputs** (not read back from the
  blobs): per thread/namespace, each checkpoint's id, parent, channel values, metadata and pending
  writes in replay order.
* `environment.json`: exact versions and the source artifact.

## Scenario

`generate_fixtures.py` drives the saver with fixed ids/timestamps and a seeded `random` (for the saver's
channel-version suffix): three checkpoints per thread; threads `thread-sync` (namespaces `""` and
`sub|child:1`) and `user/42 é` written by the **synchronous** saver, `thread-async` (two namespaces)
by the **native-async** saver; per checkpoint three tasks' pending writes with task paths, plus an
`__interrupt__` special-channel write on the last one; a channel with a version but no value
(`empty` blob).

## Regenerating (maintainers only; never edit or regenerate a committed past set)

```bash
# past release: throwaway venv with the pinned wheel
python3.12 -m venv /tmp/v881-0.12.2
/tmp/v881-0.12.2/bin/pip install "azure_functions_langgraph-0.12.2-py3-none-any.whl[azure-blob]" \
    "langgraph==1.2.11" "langgraph-checkpoint==4.2.0" "langchain-core==1.6.2" aiohttp
/tmp/v881-0.12.2/bin/python tests/fixtures/blob_compat/generate_fixtures.py \
    --label 0.12.2 --source-ref "pypi:<wheel filename> sha256=<digest>" \
    --out tests/fixtures/blob_compat/sets/0.12.2

# current tree
PYTHONPATH=src python tests/fixtures/blob_compat/generate_fixtures.py \
    --label current --source-ref "git:<commit>" --out tests/fixtures/blob_compat/sets/current
```

A new fixture set must be registered in `STORED_HINT_FORMATS` in `tests/test_blob_format_compat.py`.
`reverse_read_probe.py` is the manual downgrade probe described in the docs; it is not run by CI.
Tests never execute the generator against Azure and need no network; they exercise the real saver over
in-memory containers (the stand-in `azure.storage.blob` modules the other Blob tests use). Real Azure
Blob or Azurite behavior is not covered here.
