# API and Native HTTP contract gate

Pull requests are checked against the contract of the last published release
so an accidental breaking change cannot merge unnoticed. The gate is small,
offline and deterministic: it needs no network, secrets or cloud backends.

```bash
python tools/api_contract.py check --source src        # source tree
python tools/api_contract.py check --wheel dist/*.whl   # a built wheel artifact
```

CI runs both forms (`Lint, type-check & security` for source, `Wheel install and
graph smoke` for the built wheel). The report lists **added**, **removed** and
**changed** APIs and is also written to the job summary.

## What is compared

| Contract | Source of truth | How it is observed |
| --- | --- | --- |
| Python API | `tests/contracts/api_baseline.json` | Static `ast` extraction: `__all__`, public module paths, function signatures, class members and fields, type-alias values. Nothing is imported, so optional extras (`otel`, `streaming`, storage backends) are never required. |
| Native HTTP | `tests/contracts/native_http_baseline.json` | The real registered Functions handlers are run offline against in-memory fake graphs. Routes, methods, auth levels, status codes, content type, JSON body shape and SSE frame shape are recorded. |

Values are reduced to their structure (`string`, `integer`, ...); only the
fixed `status` and `error` strings keep their value. Platform compatibility
routes are not part of the Native HTTP fixtures.

griffe was evaluated and not adopted: it would add a pinned dev dependency and
still not cover the HTTP fixtures, while stdlib `ast` is sufficient for the
supported surface. Revisit it if inheritance resolution becomes necessary.

## Tiers

Each symbol is reported in its declared tier (mirroring the
[README stability table](https://github.com/yeongseon/azure-functions-langgraph-python#stability-rolling-0x-per-surface);
the map is `TIER_RULES` in `tools/api_contract.py`):

| Tier | Modules | Breaking change |
| --- | --- | --- |
| `stable` | `app`, `contracts`, `protocols`, `authorization`, `openapi` and the Native HTTP routes | **Blocks** unless classified |
| `beta` | `checkpointers`, `locks`, `observability`, `observability_otel` | **Blocks** unless classified |
| `experimental` | `platform`, `durable`, `streaming`, `streaming_authorization`, `triggers`, `stores` | Reported, never blocking, never certified stable |
| `unclassified` | anything else | Reported, never blocking, never certified stable |

The gate makes no 1.0 promise: the baseline is the rolling 0.x release line and
experimental differences stay in the experimental section of the report.

## Additive versus breaking

Additive (note only): new exports, modules, members, optional parameters
appended last, relaxed defaults, new JSON response fields, new routes or SSE
events. Breaking: removed or renamed symbols, parameters, fields, routes and
events; new required parameters or fields; parameter reorder, kind, annotation
or default changes; return annotation or async-ness changes; removed base
classes; a new method on a `Protocol` (breaks implementers); changed status,
content type or auth level; changed JSON value types.

## Classifying a breaking change

A breaking change in `stable`/`beta` fails until
`tests/contracts/classifications.json` has an entry for its exact change id (as
printed in the report):

```json
{
  "id": "removed:azure_functions_langgraph:SomeSymbol",
  "baseline": "0.13.0",
  "classification": "breaking",
  "refs": ["#123"],
  "rationale": "why the change is intentional",
  "migration": "what callers must do"
}
```

`classification` is `breaking`, `security-breaking` (an intentional tightening,
for example of access) or `compatible` (a flagged difference that is verified
not to break supported callers). `refs` must be issue or PR references,
`rationale` is required, and breaking kinds also need `migration`. Entries only
apply to the baseline `version` they name, so they expire when the baseline is
re-pinned. The entry does not replace the Conventional Commit `!` marker; follow
[breaking-change classification](contributing.md) for the PR title.

## Regeneration policy

The baselines describe a **published release**, never the pull request under
review. Regenerating them is only done when pinning a new release:

```bash
python tools/api_contract.py snapshot --wheel <downloaded wheel> \
    --version X.Y.Z --ref vX.Y.Z --commit <tag commit> --published-sha256 <registry digest>
# or, when the wheel cannot be downloaded, from the tag's source tree:
python tools/api_contract.py snapshot --source <git archive of vX.Y.Z>/src ...
```

Running `snapshot` cannot approve anything by itself. `check` (and
`tests/test_api_contract.py`) also require `baseline_reviews` entries in
`classifications.json` carrying the **sha256 of the committed baseline bytes**
and an issue/PR reference. Editing or regenerating a baseline without a matching
review entry fails CI, so the baseline and its classification record must change
in the same reviewed diff. Do not edit a baseline to make a PR pass; record the
breaking change in `changes` instead.

The repository has no `CODEOWNERS` file. Maintainers review any diff touching
`tests/contracts/` as a high-risk contract change; add a `CODEOWNERS` rule for
that path if a second maintainer is ever added.

The current baseline was regenerated from the **published** `0.13.0` wheel
(sha256 `4c4c9241...1d91`, equal to the registry digest; provenance `kind`
`published-wheel`). It was first pinned from the `v0.13.0` tag source (commit
`c79ea8331dfb4fe444f0c61ce54e049fc9cc3c9b`) because the registry file host was
unreachable from the generating machine; the wheel-derived payload is
byte-identical to that tag-source baseline for both contracts, so only the
provenance changed (#879).

## Verifying the baseline against the published wheel

```bash
python tools/api_contract.py verify-baseline
```

The command reads the registry metadata for the baseline version, downloads the
published wheel (retrying, https PyPI hosts only, size-capped), requires its
sha256 to equal the registry digest and the baseline provenance
`published_wheel_sha256`, then regenerates both snapshots from the unpacked wheel
and compares them byte-for-byte (payload, not provenance) with the committed
baselines. Exit codes: `0` identical, `1` differs (the differing changes are
listed), `2` unreachable or malformed input. Offline use:
`--wheel <file> --expected-sha256 <digest>`.

The weekly / manual workflow `API Contract Baseline Verification`
(`.github/workflows/api-contract-verify.yml`) runs it across a Python and
LangGraph / azure-functions matrix with `contents: read` only, no secrets and no
pull-request trigger. It is advisory and not part of `ci-required`; a failure
means the baseline provenance, or the supported dependency window, needs a
review, never an automatic baseline edit. An exit code `2` is infrastructure
(registry unreachable) and should be re-run.
