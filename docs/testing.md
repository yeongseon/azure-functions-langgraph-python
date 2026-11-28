# Testing

## Running tests

```bash
make test
```

This runs pytest with coverage reporting. The minimum coverage threshold is 95%.

## Example verification levels

The repository uses four distinct checks. Passing one does not imply the others:

| Level | What it proves | Entry point |
| --- | --- | --- |
| Import smoke | Example modules compile and register expected Python objects in the repository test environment. It does not resolve the example's standalone requirements. | `make test` (`tests/test_examples_smoke.py`) |
| Dependency resolution | A fresh Python 3.11 virtual environment can install each primary example's complete `requirements.txt`, pass `uv pip check`, import its fake-model route, and construct the real Azure OpenAI client under a mock without credentials or requests. | `make certify-examples-published` |
| Functions host smoke | The real Azure Functions Core Tools host starts and serves a native graph route from the dedicated host-smoke app. It also boots the 5-minute tutorial app (`examples/simple_agent`) with its own `host.json` and `local.settings.json` (an empty `AzureWebJobsStorage`, no emulator) and checks the calls the tutorial documents. The other examples are not started. | CI `host-smoke` job |
| Cloud certification | The release candidate is deployed to Azure and exercised over live HTTP. This is the release workflow's `azure-e2e` gate, not a pull-request dependency check. | `.github/workflows/publish-pypi.yml` |

The primary-example dependency lane has two modes:

```bash
# Newcomer path: install each checked-in requirements file unchanged from PyPI.
make certify-examples-published

# Development path: keep every example dependency, but replace only the adapter
# requirement with the supplied locally built wheel.
make build
make certify-examples-candidate WHEEL=dist/azure_functions_langgraph-*.whl
```

Both modes write resolver reports, installed versions, fake-route output, and
mocked real-model construction output under the system temporary directory by
default; CI supplies an explicit artifact path and uploads those files. A
deliberately incompatible fixture also has to fail resolution, proving the gate
detects conflicts rather than merely running commands. Neither mode invokes
Azure OpenAI or requires Azure credentials.

## Test levels

Choose the cheapest level that can actually prove the claim. A mocked test never certifies real Azure behavior, and each level below says what it does not prove.

| Level | Proves | Does not prove | Where / command |
| --- | --- | --- | --- |
| Mocked app contracts | HTTP status codes, request validation, response shapes, route registration and error handling of `LangGraphApp`, using the fake graphs described below | Real graph execution, real storage, Azure behavior | `tests/test_app.py`, `tests/test_streaming_*.py`, `tests/test_native_*.py` via `make test` |
| Deterministic storage tests | Checkpointer, thread-store and lock logic against in-memory fakes of the Azure SDK clients (for example `tests/test_checkpointers_azure_blob.py`, `tests/test_stores_azure_table.py`, `tests/test_locks_*.py`) | Real service semantics: latency, throttling, lease timing, eventual consistency | `make test` |
| Emulator / service-backed integration | The same stores against Cosmos DB or Azurite. Service-dependent (`-m integration`): `make test` selects these tests (its default marker expression excludes only `e2e`), so they run when the service is reachable and skip otherwise. The dedicated targets start the service, set `COSMOS_REQUIRED` / `AZURITE_REQUIRED` to `true` and fail on a skip | Real Azure Storage or Cosmos DB | `make test-cosmos`, `make test-table` (`tests/integration/`) |
| Functions host smoke | The real Azure Functions Core Tools host starts and serves a native graph route from `tests/host_smoke/`, and starts `examples/simple_agent` unchanged and answers the routes, health probe, invoke and stream calls the 5-minute tutorial documents | Every other example, the package version pinned in an example's `requirements.txt` (the host runs the current source), or any cloud behavior | `make host-smoke` (CI `host-smoke` job) |
| Functions host lanes | Real Core Tools hosts over one Azurite: two `examples/e2e_app` hosts sharing a storage account run the two-app lease-lock e2e (one winner, a `409` loser, checkpoint state shared between the hosts); a Platform app with a Blob checkpointer, a Blob lease lock and a Table thread store serves the scenarios of `tests/test_sdk_compat.py` through the real `langgraph-sdk` client over real HTTP (`tests/host_lanes/`); and a distributed declaration (`AZFUNC_LANGGRAPH_LOCK_BACKEND` / `AZFUNC_LANGGRAPH_THREAD_STORE`) paired with a process-local lock, thread store or checkpointer is refused at indexing and serves no route. Each pytest run is validated by `tools/check_junit.py`, so a skip fails the lane | Azure Storage behavior (latency, throttling, lease timing, API versions), a deployed or scaled-out host, a hosting plan, managed identity or any cloud setting. It is local evidence, not certification | `make host-lanes` (non-required `.github/workflows/ci-host-lanes.yml`) |
| Azure / Durable certification | The release candidate deployed to real Azure and exercised over live HTTP (`tests/e2e/`, marker `e2e`); exercises native routes (health, invoke, stream), cross-app lock and checkpoint persistence, and true streaming | Durable replay and recovery after a host interruption (neither is exercised), or anything else outside those e2e routes | `make e2e-azure` and the `azure-e2e` gate in `.github/workflows/publish-pypi.yml` |

Negative-path tests (invalid JSON, a graph that raises, a missing graph state) use `FakeFailingGraph`, `FakeFailingStatefulGraph`, `FakeNotFoundStatefulGraph` and `FakeFailingUpdateStateGraph` at the mocked-contract level. Stateful tests (`get_state`, `update_state`, state history) use `FakeStatefulGraph`. These fakes return configured values; they do not execute a graph, so they cannot verify replay or checkpoint-resume semantics. No level currently certifies Durable replay or interruption recovery; do not claim them from these tests.

The Azure Table benchmark (`tools/benchmark_table_store.py`) is a measurement tool, not a test level. Its `fake` backend models no network or service latency, and its `azurite` backend measures SDK plus emulator latency only. Neither is Azure Storage: do not quote those numbers as Azure measurements or derive a supported thread count from them. The optional real-Azure mode measures only the endpoint and network path where you run it.

The Blob checkpointer benchmark (`tools/benchmark_blob_checkpointer.py`) counts the requests `AzureBlobCheckpointSaver` issues, by kind (GET, HEAD, PUT, create-only PUT, LIST, DELETE). By default it counts against an in-memory counting fake; `--backend azurite` runs the same workload through the real `azure-storage-blob` sync and async clients on Azurite and counts the HTTP requests at the transport, so requests the SDK makes itself are included and anything unexpected shows up as its own column. `--latency-ms` adds a fixed simulated delay per request in either offline backend. Neither fake nor Azurite is Azure Storage: do not derive service latency or throughput from them. `--backend azure --connection-string-env VAR` uses the same counting transports against real Azure, reads credentials only from the named environment variable, and reports measured `wall_ms`; injected latency is prohibited there. `tests/test_benchmark_blob_checkpointer.py` pins the request budgets and the backend's classification, and `tests/integration/test_checkpoint_conformance.py` (the `make test-table` lane) requires the fake and the real SDK to count the same requests for every scenario.

### Real-Azure Blob comparison — 2026-11-24 (initial partial evidence for #860)

**Scope and limits first:** real Azure Blob Storage, one temporary Standard_LRS
StorageV2 account in `koreacentral`, Python 3.11.15, `azure-storage-blob` 12.31.1,
`azure-core` 1.41.0. Latency is **client-to-Azure from a local workstation, not
in-region**. No injected delay. Requests are counted at the HTTP transport, including
SDK retries and started list pages; setup, seeding and container deletion are outside
the per-operation counts. Counts do not depend on client location; latency does.
Payloads are the script's small structured values, not large application payloads.
No throughput, production capacity, service-side retry or throttling claim is made.

Compared `3a1c92f5a2f0fea68344c65f224da8df873a2d23` (parent of #874) with
`1fd7acb48824918a92caa2c2e58679dff9b4a0d5` (current base), using the identical
script at tooling commit `6db908f`. This includes intervening correctness changes,
not an isolated causal measurement of each optimization. Small means channels=6,
pending writes=3, history=2; medium means 20/10/5. Each API starts with a separately
seeded container. All seven scenarios execute in the script's fixed order.

**Interrupted sampling:** three alternating before/after repetitions per size were
planned. One complete pair per size and a second small-after invocation survived.
The Linux process sampler then received `PermissionError` reading the child's
`/proc/<pid>/fd`; it was not bypassed. The shell's EXIT trap deleted the resource
group immediately. No second group was created. Therefore the following are
**individual observations, not medians**; repeated before/after variance is **NOT
PROVEN**. The second small-after invocation has matching request counts.

Each cell is **HTTP requests / wall milliseconds**. Small-after parentheses contain
the second invocation's milliseconds. Sync and async request counts agreed.

| Scenario | API | Small before | Small after (second) | Medium before | Medium after |
| --- | --- | ---: | ---: | ---: | ---: |
| get latest | sync | 24 / 232.025 | 13 / 135.201 (142.379) | 66 / 547.005 | 34 / 126.184 |
| get by ID | sync | 23 / 167.614 | 12 / 78.351 (78.948) | 65 / 592.969 | 33 / 91.055 |
| list limit=1 | sync | 41 / 385.852 | 13 / 126.217 (92.407) | 111 / 1015.743 | 34 / 277.787 |
| list no-match filter | sync | 41 / 328.417 | 3 / 38.173 (39.515) | 246 / 2025.630 | 6 / 84.058 |
| put new checkpoint | sync | 12 / 111.406 | 12 / 145.433 (134.661) | 26 / 269.348 | 26 / 351.091 |
| first pending writes | sync | 6 / 72.996 | 3 / 32.329 (47.808) | 20 / 171.006 | 10 / 118.194 |
| retry existing writes | sync | 3 / 28.666 | 3 / 24.880 (34.996) | 10 / 70.045 | 10 / 79.330 |
| get latest | async | 24 / 455.930 | 13 / 113.512 (187.862) | 66 / 637.502 | 34 / 156.242 |
| get by ID | async | 23 / 189.872 | 12 / 87.762 (56.901) | 65 / 567.461 | 33 / 131.206 |
| list limit=1 | async | 41 / 388.770 | 13 / 93.307 (92.566) | 111 / 1088.000 | 34 / 106.806 |
| list no-match filter | async | 41 / 358.337 | 3 / 31.771 (29.504) | 246 / 2212.543 | 6 / 63.977 |
| put new checkpoint | async | 12 / 108.459 | 12 / 132.715 (113.311) | 26 / 270.746 | 26 / 284.939 |
| first pending writes | async | 6 / 49.597 | 3 / 40.553 (32.050) | 20 / 230.737 | 10 / 109.298 |
| retry existing writes | async | 3 / 19.358 | 3 / 22.986 (25.306) | 10 / 114.175 | 10 / 83.159 |

The measured medium counts reproduce #937's emulator budgets: 66→34, 65→33,
111→34, 246→6, 26→26, 20→10 and 10→10. There is no request reduction on put or
already-written retries. Medium sync put was **30% slower in this sample** despite
unchanged requests; medium sync duplicate retries were 13% slower, whereas async
duplicate retries were faster. Sparse samples cannot establish a latency regression
or resolve the HEAD-versus-rejected-create trade-off. Do not describe every path as
faster. No shipped-runtime defect was established and no runtime code was changed.

Whole-child-process observations, sampled every 20 ms while running both APIs:

| Size/revision | Peak RSS KiB | Peak socket descriptors | Peak threads |
| --- | ---: | ---: | ---: |
| small before | 74316 | 4 | 2 |
| small after | 80800 | 9 | 8 |
| medium before | 74924 | 4 | 2 |
| medium after | 83788 | 11 | 11 |

These are Linux `/proc` samples including interpreter/import/seed/cleanup overhead,
not operation-local allocations, simultaneous HTTP requests, TCP connection counts,
pool occupancy, or exact maxima. Async event-loop sockets are included. They are
consistent with higher resource pressure from parallel reads, not proof of pool
safety. Per-operation memory and connection-pool pressure were **NOT MEASURED**.
The interrupted fifth sample has no resource-observation row.

Reproduction (with the approved temporary account already created; never enable
shell tracing, print the variable, or pass its value on argv):

```bash
export BENCH_BLOB_CS="$(az storage account show-connection-string \
  --name "$ACCOUNT" --resource-group "$RG" --query connectionString -o tsv)"
# Execute each command with the same Python/SDK environment, alternating revisions.
PYTHONPATH=/tmp/opencode/wt-860-before/src python tools/benchmark_blob_checkpointer.py \
  --backend azure --connection-string-env BENCH_BLOB_CS \
  --channels 6 --writes 3 --history 2 --json
PYTHONPATH=/tmp/opencode/wt-860/src python tools/benchmark_blob_checkpointer.py \
  --backend azure --connection-string-env BENCH_BLOB_CS \
  --channels 6 --writes 3 --history 2 --json
# Repeat with --channels 20 --writes 10 --history 5; repeat whole pairs three times.
unset BENCH_BLOB_CS
```

Actual benchmark invocations used the existing Hatch `table/bin/python` directly,
wrapped with `timeout 240` and an external standard-library `/proc` sampler.
The connection string was acquired only in the same shell invocation as the runs.
Raw JSON and the credential-free sampler/runner remain locally in
`/tmp/opencode/bench860/`; this directory is not a permanent published artifact.

**Cleanup:** the only resource group was `rg-af-bench-860-202611241425`, containing
only account `stafbench860d7262d` (public Blob access disabled, minimum TLS1_2).
The EXIT trap ran `az group delete --yes --no-wait`, then `az group wait --deleted`.
`az group exists --name rg-af-bench-860-202611241425` returned `false` and
`az group list --query "[?starts_with(name,'rg-af-bench-')].name" -o tsv` returned
empty, captured in `deleted.txt` and `remaining-groups.txt`. Storage-only use lasted
minutes, not hours. Estimated cost is **under US$0.10**, not an Azure billing report;
no compute was provisioned.

**Still not proven / decisions:** recovery, actual lease loss, SDK 503 retry/small
pool behavior, cancellation, concurrent slot writes and historical fixture replay
were not run against Azure before cleanup. Existing offline tests are not live
proof. The newest #860 comment (#949) explicitly leaves those items open and records
that #905 rejects a lost-owner duplicate retry where the older code skipped it:
whether that meets the historical "unchanged ownership guards" criterion **needs a
maintainer decision**. The unchecked historical description of sequential reads and
existence-check writes is stale; #874/#905/#909/#937/#949 already merged. Existing
strict-xfail tests also document lower-hint concurrent-writer and pre-0.13
mixed-fleet crash gaps; this evidence does not remove either limit, promise fencing,
or justify closing #860.

### Real-Azure Blob continuation — 2026-11-24 (still partial, #860)

**Scope and limits first.** The continuation completed three independent before/after
pairs per size and the existing Blob integration suite on one new temporary Azure account.
Latency is **client-to-Azure from a local workstation, not in-region**. This supersedes
the initial run's single-pair timing limitation, not its honest interruption record above.
It does **not** complete the broader fault/lease/compatibility acceptance: a supplemental
scratch harness failed before those probes could supply usable evidence. No `src/`
behavior changed, no issue boxes changed, and this remains partial evidence.

**Method.** Same tooling `6db908f`, before `3a1c92f5a2f0fea68344c65f224da8df873a2d23`,
after `1fd7acb48824918a92caa2c2e58679dff9b4a0d5` as the initial PR run; the latter is
the original comparison baseline, **not today's main**. Conformance used evidence branch
`acbe9e1`, after mechanically merging main `f13ad5d`. Python 3.11.15, Blob SDK 12.31.1,
azure-core 1.41.0, conformance harness 0.0.2. Small is channels/writes/history 6/3/2;
medium is 20/10/5. Each invocation seeds separate sync/async containers, executes the
same seven scenarios in order, and deletes them. Revision order is before/after for
repetitions 1 and 3, after/before for repetition 2. All 12 invocations completed:
168 operation observations. No injected latency. Counts exclude setup/seed/cleanup,
include transport sends, and were identical across all three repetitions per cell.

**Location-independent request counts and workstation latency are separate columns.**
Times are median/max milliseconds of exactly three samples; they are not percentiles
or throughput. Small structured payloads are not a production workload certification.

| Size | Scenario | API | Requests before→after | Median/max ms before | Median/max ms after |
|---|---|---|---:|---:|---:|
| small | latest | sync | 24→13 | 189.246/199.705 | 94.044/96.367 |
| small | by ID | sync | 23→12 | 164.712/174.497 | 63.757/123.419 |
| small | list limit=1 | sync | 41→13 | 298.584/348.232 | 95.150/96.221 |
| small | no-match filter | sync | 41→3 | 318.346/324.524 | 31.945/32.790 |
| small | put new | sync | 12→12 | 114.900/134.790 | 134.214/147.906 |
| small | first writes | sync | 6→3 | 50.750/53.754 | 32.065/33.408 |
| small | duplicate retry | sync | 3→3 | 21.147/22.105 | 26.356/26.745 |
| small | latest | async | 24→13 | 200.567/204.186 | 110.518/121.057 |
| small | by ID | async | 23→12 | 169.759/185.910 | 67.562/68.358 |
| small | list limit=1 | async | 41→13 | 309.854/328.867 | 64.753/67.623 |
| small | no-match filter | async | 41→3 | 314.199/318.503 | 30.736/31.475 |
| small | put new | async | 12→12 | 121.482/125.687 | 119.845/137.930 |
| small | first writes | async | 6→3 | 48.305/58.456 | 31.530/33.747 |
| small | duplicate retry | async | 3→3 | 19.769/22.958 | 24.019/26.062 |
| medium | latest | sync | 66→34 | 537.459/540.514 | 148.250/160.563 |
| medium | by ID | sync | 65→33 | 529.645/539.875 | 97.566/102.373 |
| medium | list limit=1 | sync | 111→34 | 889.320/895.938 | 158.802/166.819 |
| medium | no-match filter | sync | 246→6 | 1991.650/2077.053 | 59.277/102.212 |
| medium | put new | sync | 26→26 | 284.364/286.812 | 256.470/283.463 |
| medium | first writes | sync | 20→10 | 170.660/188.769 | 103.463/114.188 |
| medium | duplicate retry | sync | 10→10 | 69.277/71.442 | 81.653/94.043 |
| medium | latest | async | 66→34 | 550.981/558.394 | 149.302/170.001 |
| medium | by ID | async | 65→33 | 494.786/501.788 | 98.010/98.344 |
| medium | list limit=1 | async | 111→34 | 883.927/921.034 | 149.146/157.066 |
| medium | no-match filter | async | 246→6 | 1928.636/1953.930 | 63.967/114.295 |
| medium | put new | async | 26→26 | 268.866/271.105 | 295.882/305.836 |
| medium | first writes | async | 20→10 | 192.776/389.905 | 113.381/125.967 |
| medium | duplicate retry | async | 10→10 | 69.412/73.451 | 78.410/79.981 |

Duplicate retries are slower in all four sampled size/API medians, with unchanged
request counts: medium sync +17.9%, async +13.0%. Small sync put is slower; medium
sync put is faster, while medium async put is slower. These observations do not
establish a causal regression or universal speedup; HEAD versus rejected-create
remains a maintainer trade-off, not grounds for a runtime edit in this evidence PR.

**Safe resources.** The wrapper runs the unchanged benchmark in its own process and
records only `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss` in `finally`.
It never inspects child/other-user `/proc` entries. Values include imports, seed,
both APIs and cleanup, not operation-local allocations. Connections, sockets,
pool occupancy, threads and per-operation memory are **NOT MEASURED** in this run.

| Size/revision | Peak RSS KiB samples 1/2/3 | Median/max KiB |
|---|---|---|
| small before | 73692/73660/73420 | 73660/73692 |
| small after | 79576/79604/79460 | 79576/79604 |
| medium before | 73588/73448/73712 | 73588/73712 |
| medium after | 82428/82016/82268 | 82268/82428 |

**Existing live integration/conformance: 10 passed, 0 failed, 0 skipped, 38.44 s.**
`BLOB_TEST_CONNECTION_STRING_ENV=BENCH_BLOB_CS` selects the named credential for
all sync/native-async clients and benchmark checks; unset retains Azurite. Missing
or empty named credentials fail with a value-free message, never an emulator fallback.
Four offline guard cases cover selection, failure sanitization and unchanged defaults.

| Existing scenario | Passed | Failed/skipped | What it demonstrates |
|---|---:|---|---|
| `test_conformance_base_capabilities_azurite` | 1 | 0/0 | Upstream put/writes/get/list/delete via sync-forwarding wrapper |
| `test_native_and_fallback_conformance_azurite[False/True]` | 2 | 0/0 | Upstream contract via executor fallback and native async |
| `test_latest_publication_failure_recovers_on_azurite[sync/native/fallback]` | 3 | 0/0 | Real committed marker, client-injected failure before clean hint; fresh reader recovers B, retains A/history |
| `test_benchmark_fake_issues_the_requests_the_real_sdk_sends[6-3-2/20-10-5 × sync/async]` | 4 | 0/0 | All seven fake request budgets match actual Azure sends |

The legacy test names still say Azurite; the explicit selector and account prove the
backend here was Azure. No emulator-only scenario was silently omitted. Conformance
is a sampled API contract, not all failure schedules or multi-process certification.

**Supplemental harness: 0 passed, 4 failed; not product failures or live proof.**
`extra.xml`/`extra.log` preserve the failures verbatim. Three async cases (the old
scratch recovery/conflict/timeout/fixture runner and two cancellation cases) were
collected outside the repository configuration, so pytest used strict rather than
auto asyncio mode: `async def functions are not natively supported.` Their bodies
did not execute. The lease probe acquired/contended and wrote ordinary state, then
failed at `BlobClient.break_lease` with
`AttributeError: 'BlobClient' object has no attribute 'break_lease'` (the operation
belongs to `BlobLeaseClient`). Thus it did not break a lease or verify loss detection.
The EXIT trap cleaned up before this was investigated. No second group was created;
no runtime change or Azure-vs-Azurite saver defect is established by these harness errors.

**Evidence map (issue unchanged).**

| #860 acceptance / remaining requirement | Status after continuation |
|---|---|
| Realistic counts/pending writes, simulated latency, sync/async baseline | **PROVEN for sampled counts**: prior fake/Azurite coverage plus 168 live observations; large payload/production realism NOT PROVEN |
| Bounded parallel reads, deterministic order and cleanup | **NOT PROVEN live** as a complete invariant; upstream conformance passes, but neither an eight-read cap nor pool/cancellation safety was measured |
| limit=0 / stop before over-building / metadata prefilter | **PROVEN sampled reductions** for limit=1 and no-match across three repetitions; limit=0 remains offline/emulator proof only |
| Conditional first-write keys and preserved mapped-write semantics | **PROVEN sampled requests** and upstream put_writes contract; explicit racing/mapped-overwrite fault matrix NOT PROVEN live |
| Dirty intent / commit marker / clean hint / ownership / recovery unchanged | **NEEDS MAINTAINER DECISION**: newest issue comment records #905 rejecting a lost-owner duplicate retry previously silently skipped. Publication/recovery now has the three sampled live clean-hint-failure recoveries; actual lease loss and the full invariant remain NOT PROVEN |
| Sync/async conflict, transient failure, cancellation, retry conformance | **PROVEN sampled ordinary upstream interoperability and clean-hint recovery only**; full item NOT PROVEN: scratch conflict/cancellation runner did not execute, no SDK 503/small-pool probe or service-generated transient failure |
| Before/after requests and latency | **PROVEN for three repetitions per size/API**, median/max above; in-region latency NOT PROVEN |
| Real-Azure memory / connection pressure | **PROVEN whole-process peak RSS only** using self usage; connections/pool pressure/per-operation memory NOT MEASURED |
| Rolling compatibility | **NOT PROVEN live**: historical fixture scratch replay did not execute; existing offline/strict-xfail limitations remain, no fencing or downgrade promise |

**Exact execution and cleanup.** The existing logged-in default subscription was
verified as `Visual Studio Enterprise Subscription`, `Enabled`; no login/subscription
change or credentials on argv. The connection string existed only in a shell variable
obtained in the same invocation as benchmarks/tests. The commands below show names,
never credential values. The safe self-usage wrapper and exact scratch driver, raw JSON,
resource samples, JUnit and logs remain under `/tmp/opencode/bench860b/` locally, not
a permanent public artifact. The benchmark file was extracted unchanged from `6db908f`;
before/after source trees were extracted with `git archive` at the two recorded SHAs.

```bash
export BENCH_BLOB_CS="$(az storage account show-connection-string \
  --name "$ACCOUNT" --resource-group "$RG" --query connectionString -o tsv)"
# Three pairs per size; second pair reverses order. PY is the reused Python 3.11.
PYTHONPATH=/tmp/opencode/bench860b/before/src timeout 240 "$PY" \
  /tmp/opencode/bench860b/sample_self.py small-before-1.resources.json \
  --backend azure --connection-string-env BENCH_BLOB_CS \
  --channels 6 --writes 3 --history 2 --json
# Corresponding after/src; medium uses --channels 20 --writes 10 --history 5.
BLOB_TEST_CONNECTION_STRING_ENV=BENCH_BLOB_CS timeout 900 "$PY" -m pytest \
  -v -m integration tests/integration/test_checkpoint_conformance.py --no-cov \
  -p no:cacheprovider --tb=short --show-capture=no \
  --junitxml=/tmp/opencode/bench860b/conformance.xml
# Failed scratch invocation (outside repo config; retained, not counted as proof):
BLOB_TEST_CONNECTION_STRING_ENV=BENCH_BLOB_CS timeout 900 "$PY" -m pytest \
  -v -s -m integration /tmp/opencode/bench860b/test_live_extra.py --no-cov \
  -p no:cacheprovider --tb=short --show-capture=no \
  --junitxml=/tmp/opencode/bench860b/extra.xml
unset BENCH_BLOB_CS
```

Created only `rg-af-bench-860b-202611241531` in `koreacentral`, tagged
`purpose=benchmark repo=azure-functions-langgraph issue=860 owner=agent`, with one
Standard_LRS StorageV2 account `stafbench860b328770` (public Blob access false, minimum TLS1_2).
Start `2026-11-24T15:31:22Z`; final deletion proof `2026-11-24T15:35:00Z`.
EXIT trap ran synchronous `az group delete --yes`, then `az group wait --deleted`.
`az group exists --name rg-af-bench-860b-202611241531 -o tsv` returned **false**;
`az group list --query "[?starts_with(name,'rg-af-bench-')].name" -o tsv` returned
**no names** (`deleted.txt`, `remaining-groups.txt`). No compute provisioned.
Conservative estimate **under US$0.10** for a few minutes of small-payload storage
and fewer than 50,000 planned requests; session-wide sends were not instrumented.
This is a budget estimate, not an invoice or verified regional tariff, well under $5.

**Continuation validation.** `make lint` timed out after 45 seconds while Hatch tried
to create/install the new worktree environment. Used the previous run's existing
Python 3.11 Hatch interpreters directly, not a new successful Make gate: ruff check
and format-check passed, all three workflow linters passed, default-environment mypy
passed (301 files), and MkDocs built the final evidence prose (existing link notices).
`make build` passed. The full source suite, with the previous locally built 0.14.2
wheel target providing correct distribution metadata, passed: **5408 passed,
107 skipped, 37 deselected, 2 xfailed; coverage 96.91%**. The 107 skips are optional
local-service/dependency tests, not skipped live Blob scenarios. Focused installed-wheel
configuration: **36 passed** with `tools/wheel-tests.ini`, no source-tree PYTHONPATH.
An initial direct mypy run in the Table-extra environment reported one new test
re-export error (corrected by importing importlib directly) and three unrelated
host-lane SDK/protocol errors; the normal default environment mypy passed.
LSP is clean for the new guard file; the integration module retains existing
unresolved local-package and `_HAS_CONFORMANCE` constant-redefinition diagnostics.
Read-only advisory review found no concrete defect; it is not human approval or CI.

### Real-Azure fault follow-up — 2026-11-24 (#860, still partial)

**8 passed, 0 failed/skipped in 45.04 s**, once at `561ad6b`, using repository
`pyproject.toml` (asyncio auto mode), not the failed scratch harness. Latency is
**client-to-Azure from a local workstation, not in-region**. New
`tests/integration/test_blob_live_faults.py` skips before resolving its network
fixture unless `BLOB_TEST_CONNECTION_STRING_ENV=BENCH_BLOB_CS` is set. Cancellation
mirrors the offline schedule at the real saver upload seam; it is not cancellation
at an arbitrary TCP packet boundary. Fresh savers read through both APIs.

| Live probe | Result / observed timing |
|---|---|
| Native cancellation before/after `checkpoint.bin` (2) | PASS: before marker no checkpoint is visible; after marker the complete checkpoint is visible. Dirty hint persists; explicit retry cleans it and preserves values, metadata, slots and history. Whole cancellation/recovery/retry schedules: 0.603/0.740 s. |
| **CLIENT-INJECTED** lost marker response, sync/native (2) | PASS: real upload succeeds, injected `TimeoutError` propagates, fresh reader recovers the committed tuple, explicit retry repairs hint, duplicate slots retain originals. Whole schedules: 0.575/0.499 s. Not an SDK retry/503 test. |
| Real lease break, guard off, sync/native (2) | PASS: `BlobLeaseClient(marker).break_lease(lease_break_period=0)` frees `thread-locks/live/t`; a separate lock acquires it. Normal next renewal returns HTTP 409 `LeaseIdMismatchWithLeaseOperation`; loss detected 4.945/4.957 s after break response. Stale checkpoint writes still succeed. |
| Real lease break, guard on, sync/native (2) | PASS: same real break/successor; loss detected after 4.924/4.970 s. Stale writes raise `ThreadLeaseLostError(reason="lost")` and leave checkpoint blobs unchanged. |

Break calls took 0.009–0.039 s; leases were 15 s with normal 5 s renewal cadence,
no accelerated/fake clock or injected renewal error. Detection times include up
to 20 ms polling granularity. Observations match the production guide: the marker
lease does not fence checkpoint writes; the opt-in guard rejects only **detected**
loss. No documentation mismatch observed. Service-generated 503 cannot be induced
on demand and was not observed. Read-concurrency/pool safety, rolling compatibility
and the full conflict matrix remain **NOT PROVEN live**; pool pressure remains
**NOT MEASURED**. The #905 ownership contract still **NEEDS MAINTAINER DECISION**.

Reproduce on an approved temporary account with credentials acquired only into a
shell variable in the same invocation: `BLOB_TEST_CONNECTION_STRING_ENV=BENCH_BLOB_CS
timeout 600 "$PY" -m pytest -v -s -m integration
tests/integration/test_blob_live_faults.py --no-cov -p no:cacheprovider --tb=no
--show-capture=no --junitxml=/tmp/opencode/bench860c/live.xml` (join wrapped command
lines). Raw safe runner, JUnit and logs are local under `/tmp/opencode/bench860c/`,
not permanent published artifacts. Python 3.11.15 reused the existing Hatch `table`
environment; source plus the prior local 0.14.2 wheel target supplied metadata.

Only `rg-af-bench-860c-202611241627` / `stafbench860c88cdf3` was created, in
`koreacentral`, tagged `purpose=benchmark repo=azure-functions-langgraph issue=860
owner=agent`; Standard_LRS StorageV2, public Blob access false, TLS1_2. Lifetime
16:27:30–16:30:37 UTC. EXIT trap synchronously deleted the group and waited;
`deleted.txt` is `false`, `remaining-groups.txt` is empty. No compute. Estimated
cost **<US$0.10** (minutes of tiny-payload storage/requests, not an invoice).

Validation: affected offline tests **15 passed/8 opt-in skips**; installed-wheel
selection **37 passed/8 opt-in skips**. Full source suite **5407 passed, 117 skipped,
37 deselected, 2 xfailed; 96.91% coverage** (optional-service/dependency skips).
`make build` passed. `make lint` timed out at 45 s; format-check/typecheck/
lint-workflows/docs/test timed out at 20 s during Hatch dependency setup. Direct
existing-environment ruff check/format, mypy (303 files), three workflow linters
passed; these are fallback results, not successful Make gates.
Direct MkDocs build passed (existing link notices). Both new files have clean LSP
diagnostics with a temporary local Python 3.11/source-path configuration (not
committed). Subsequent edits only make the same guarded configuration type-safe;
the live run remains attributed to its actual tested revision, not a later SHA.

### In-region storage attempt — 2026-11-28 (#801 and #860)

**Final authorized session: partial real in-region evidence.** The ACI completed
with exit 0 after the smoke gate, two complete Table scale cells, twelve Blob
comparison runs and the Blob pressure/compatibility probe. The 100k Table and
sustained-writer cells timed out without result payloads; they remain unproven.
Earlier attempts were launcher failures, not adapter failures. This record
replaces their no-measurement diagnosis; the prior commits preserve that history.

| Item | Observed or requested value |
| --- | --- |
| Candidate | `origin/main` at `03cfda4f451e405e4087f40d79cd28df7dc6c48e`; unchanged version `0.14.2` |
| Wheel | Built locally with `uv build --wheel`; sha256 `fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3` |
| Local tooling | Python 3.11.15 venv created with `uv`; Azure CLI 2.83.0, uv 0.11.7 |
| Compute request | One Linux ACI, `python:3.11-slim-bookworm`, 1 CPU, 1.5 GiB, `restartPolicy=Never`, `koreacentral`; **no Functions hosting plan** |
| Network and storage request | No IP address or ports in the ACI resource request; no SSH keys or inbound NSG rules; one StorageV2 Standard_LRS account in the same group/region, public Blob access disabled, TLS1_2 |
| Packaging | Private bundle containing the candidate wheel under `wheels/`, existing repository tools and committed compatibility fixtures; session command selected the local wheel, not PyPI |
| Credential handling | Account key and connection string passed as ACI secure environment values through an ARM request on stdin; private SDK bundle download, no SAS/public blob, secret settings file, role assignment, Entra object, Insights or workspace |
| Installed runtime | Python **3.11.18**, Blob SDK 12.31.1, Table SDK 12.7.0, azure-core 1.41.0, LangGraph 1.2.24, langgraph-checkpoint 4.3.2, requests 2.34.2, aiohttp 3.14.5 |
| Image observed | `python@sha256:0f6686ddd2ccdb042d2b5b15f1cc6f015b2fdf5dc159fcccd9d4ad9d70904898` |
| Lifecycle | Request `01:40:26.945922Z`; container started `01:40:51.071Z`, finished `02:11:42.938Z`, no restarts, group `Succeeded`, container `Terminated/Completed`, exit 0 |
| Cleanup | Verified `02:13:14.809727Z`, before analysis/docs; process lifetime **30m51.867s**, request-to-deletion upper bound **32m47.864s**, below 60 minutes |

**Capture and smoke.** State was saved before each log request; polling every ten
seconds treated startup-not-ready as normal. Private bundle delivery and pip
installation succeeded. The wheel extras `[azure-table,azure-blob]` installed
successfully in **33.182 s**, then import plus tiny Table insert/get and Blob
upload/download passed in **0.532 s**, within the two-minute smoke allowance
(bootstrap Blob SDK installation was a separate preceding step). The adapter
import resolved to `/usr/local/lib/python3.11/site-packages`, not `src/`.
`SMOKE_READY` was captured before the local controller wrote the private `go`
gate. Each cell emitted sanitized JSON between `BEGIN_RESULT`/`END_RESULT`.
Full final stdout and instance state were captured **before** synchronous group
deletion; there was no results-blob upload dependency.

**Table method and scope.** Existing `tools/benchmark_table_status_index.py`,
seed 7, paired timestamps, independent seeded ordering oracle; index off/on,
three timed repetitions plus a separate `tracemalloc` pass. There were **48
mode/workload cells × four executions**, no oracle mismatches in the completed
1k/10k cells. Wire counts include SDK sends and fetched entities, not just
returned rows. These are **ACI-to-storage in-region** measurements with SDK
decoding/oracle costs; not Functions cold-start or end-to-end HTTP latency.

| Threads / idle rows | idle limit=10 scan requests/rows; median/max ms | Index requests/rows; median/max ms | Scan/index peak traced MiB |
| --- | --- | --- | --- |
| 1,000 / 814 | 1/814; 96.8/106.5 | 2/28; 19.7/21.1 | 3.9/0.1 |
| 10,000 / 8,016 | 9/8,016; 973.3/1000.8 | 2/28; 38.8/40.4 | 23.1/0.1 |

| Threads / case | Scan → index median ms | Scan → index requests/rows |
| --- | --- | --- |
| 1k interrupted limit=10 | 18.2 → 19.3 | 1/72 → 2/28 |
| 1k error limit=10 | 12.6 → 18.3 | 1/29 → 2/28 |
| 1k idle offset=100 | 95.2 → 111.8 | 1/814 → 9/224 |
| 1k idle offset=200 | 101.5 → 206.3 | 1/814 → 16/420 |
| 1k idle limit=1000 | 93.1 → 827.4 | 1/814 → 60/1628 |
| 10k interrupted limit=10 | 112.2 → 52.2 | 1/707 → 2/28 |
| 10k error limit=10 | 71.5 → 54.1 | 1/310 → 2/28 |
| 10k idle offset=100 | 1004.4 → 363.3 | 9/8016 → 9/224 normally, 10/225 with marker refresh |
| 10k idle offset=200 | 1145.7 → 695.2 | 9/8016 → 16/420 |
| 10k idle limit=1000 | 1118.2 → 3561.7 | 9/8016 → 74/2996 normally, 75/2997 with marker refresh |

Indexed limit=0 sent **zero** requests; off mode still scanned 814/8016 rows.
Offset 201 fell back to scan; busy, unfiltered, metadata-filtered and count calls
had identical off/on wire counts. At 10k, limit=1000 fetched 2996 hint/primary
entities normally rather than only the returned 1000. Indexing is not uniformly
faster: 1k rare statuses/deeper offsets and both large-limit cases regressed.

| Threads | Rebuild ms / requests / indexed hints | Clean readiness audit ms / requests | Repair / rollback |
| --- | --- | --- | --- |
| 1k | 1472.9 / 13 / 915 | 210.5 / 7 | Six stale hints, zero missing; repair 996.5 ms/79 requests deleted six; disabled read 91.2 ms/1 request |
| 10k | 10907.4 / 104 / 9033 | 972.1 / 24 | Six stale, zero missing; repair intentionally not run; disabled read 1292.5 ms/9 requests |

Three write sequences per mode/scale confirm create adds one write (1→2
requests); acquire unchanged (2); terminal release/status update 2→3 requests;
metadata/delete unchanged (2). At 10k, median off→on ms: create 8.3→13.5,
acquire 13.6→11.7, release 12.0→18.0, error update 13.1→18.6,
metadata 11.6→11.5, delete 12.5→10.5. Acquire/release remains **2→3 writes**
(+50%), 4→5 total requests. The two successful whole-tool totals were **627
and 1277** sends including seeding/admin/cleanup; not the entire session count.

**Timeout findings.** The 100k scale cell reached its **900 s** subprocess limit,
and the four-process sustained-writer cell reached **550 s**. Both captured
empty stdout/stderr, so no phase, completed cycles, consistency result, U2
comparison boundary or hot-partition rate can be inferred. The writer driver
prints only after quiescent comparisons/repair, which loses intermediate work
on timeout; it is not evidence that the service rejected writes or a proven
adapter hang. No further deployment was made. Group deletion removed any
tables/process remnants. Complete cells below still ran and were retained.

**Blob comparison.** Existing `tools/benchmark_blob_checkpointer.py`, zero injected
latency, small=6 channels/3 writes/2 history, medium=20/10/5. Three before/after
pairs per size, second pair reversed, both sync and native async: **168 measured
operation rows**. Before uses the historical saver module at `3a1c92f` (same
baseline as #937/#970) over the current dependency environment; after imports
the installed candidate wheel. This is a saver-module comparison, **not a whole
historical wheel/dependency comparison**. Setup, seed and deletion excluded.

| Medium operation | Requests before→after | Sync median/max ms before→after | Native median/max ms before→after |
| --- | --- | --- | --- |
| latest | 66→34 | 402.963/415.729 → 350.941/372.449 | 399.166/442.159 → 172.526/174.431 |
| by ID | 65→33 | 347.841/402.878 → 88.662/102.033 | 349.150/389.332 → 97.625/104.787 |
| list limit=1 | 111→34 | 615.688/715.481 → 110.925/133.358 | 601.030/622.808 → 99.270/103.892 |
| no-match filter | 246→6 | 1489.926/1615.470 → 43.015/47.019 | 1383.927/1480.582 → 45.535/48.324 |
| put new | 26→26 | 187.679/263.354 → 225.023/247.785 | 210.479/224.169 → 239.622/245.207 |
| first writes | 20→10 | 115.438/155.607 → 88.020/90.185 | 130.405/141.511 → 82.325/100.768 |
| duplicate retry | 10→10 | 46.306/67.103 → 59.389/60.129 | 46.464/47.074 → 73.263/82.428 |

| Small operation | Requests before→after | Sync median/max ms before→after | Native median/max ms before→after |
| --- | --- | --- | --- |
| latest | 24→13 | 131.075/148.055 → 206.875/243.065 | 176.223/191.876 → 105.129/141.688 |
| by ID | 23→12 | 119.202/119.409 → 53.062/122.405 | 122.896/128.053 → 46.237/70.129 |
| list limit=1 | 41→13 | 236.203/243.362 → 54.487/108.816 | 221.606/242.118 → 50.761/55.531 |
| no-match filter | 41→3 | 198.326/225.565 → 23.969/30.643 | 210.117/245.354 → 22.658/23.924 |
| put new | 12→12 | 86.128/87.911 → 88.895/98.358 | 93.626/115.923 → 89.176/124.995 |
| first writes | 6→3 | 37.586/38.965 → 23.354/25.360 | 39.306/42.042 → 22.985/32.382 |
| duplicate retry | 3→3 | 13.514/22.955 → 17.541/18.863 | 15.368/17.150 → 17.533/26.077 |

Request counts were identical across all repetitions/APIs. Lower requests did
not guarantee lower latency: **small sync latest regressed**, new-checkpoint
publication was mixed/slower, and every duplicate-retry median increased.
First operation times include client connection/concurrency startup; three
repetitions on one burstable-sized ACI are not a general latency guarantee.

**Additional real-SDK probe, 52.551 s total:**

- Two-connection sync blocking requests pool and native aiohttp pool: ten medium
  latest reads each, correct complete tuple every time, transport peak **8**,
  active calls after completion **0**, zero-limit list **0 requests**. Measured
  transport calls include pool wait, **not eight simultaneous sockets** or full
  downloader lifetime. Socket occupancy/RSS and read-cancellation cleanup were
  not measured by this probe.
- Each API substituted one HTTP **503 in the client transport** after draining
  a real successful GET response. SDK retried successfully: **341 sends** for
  ten reads (baseline 340 + one retry), sync 17.643 s, native 16.746 s. Real
  service responses were 331 HTTP 206 and ten HTTP 200. **No service-generated
  503/throttling is claimed**, and injected-retry timings are not ordinary read
  latency. Fault probes do not change production hint/publication logic.
- Two independent sync clients raced one create-only slot; one accepted value
  survived a duplicate retry. Mapped `__error__` overwrite retained the new
  value. This is one sampled slot race, not an exhaustive conflict matrix or
  native-writer race.
- Persisted wheel-generated fixtures 0.8.2, 0.9.4, 0.12.2, 0.13.0 and current
  were uploaded to private Azure containers, decoded through sync/native list
  against existing expected values, and **all blob ETags remained unchanged**.
  Counts: 69 blobs/6 lists for 0.8.2, 115 blobs/10 lists for each other set,
  **46 lists total**. This proves sampled old-data→current-reader behavior,
  **not live old/new writers, continuation, downgrade or mixed-fleet safety**.

**Acceptance audit (issue checkboxes unchanged):**

| Issue / acceptance items | This attempt adds | Remaining boundary |
| --- | --- | --- |
| #801 item 1, write-cost/consistency decision | Nothing; the maintainer's 2026-11-24 acceptance remains recorded in the issue | Experimental/default-off only, no support promotion |
| #801 items 2–4, defaults/order/bounded reads | 1k/10k seeded oracle, measured requests/rows and limit=0 | Exhaustive offsets/concurrent snapshots not established |
| #801 items 5–6, writers/failures and rollout/repair/rollback | Rebuild/audit/disable both scales, 1k repair; existing controlled races unchanged | Sustained-writer timeout, 10k/100k repair/admin/TTL unproven |
| #801 items 7–8, scan compatibility and repository gates | Sampled equal off/on scan/count results and I/O | Final-head required GitHub CI authoritative |
| #801 item 9 and U2/U4, amplification/latency and limits | In-region 1k/10k read/write samples and regressions | 100k timeout, comparison boundary/hot-partition rate unproven |
| #860 items 1 and 7, realistic baseline/before-after | Three live in-region pairs per size/API; raw 168 observations | Large byte payloads/production realism untested |
| #860 items 2–4, bounded reads/cleanup, early limits/write semantics | Sampled peak-eight calls, zero completed in-flight calls, limit=0, slot race/mapped overwrite | Read cancellation, complete concurrency/ordering and native race matrix unproven |
| #860 items 5–6, publication/ownership/conflict/faults | SDK retry on client-substituted 503; prior cancellation/lease-loss probes remain scoped | No service-generated transient fault or complete conflict/publication invariant; lost-owner duplicate decision remains open |
| #860 additional memory/connection/rolling | Small-pool completion, fixture-to-current-reader real Azure replay | Socket occupancy/RSS, old/new writer fleet, downgrade/continuation unproven |

**Cleanup evidence, saved locally.** Only the uniquely named
`rg-aflg-801-verify-*` group created by this session was deleted. The launcher's
`finally` executed synchronous `az group delete --yes`, then
`az group wait --deleted`, followed by the required checks:

```text
az group exists -n <OWN_GROUP>: false
az resource list --tag purpose=801-verify: []
new DefaultResourceGroup-* groups relative to pre-run baseline: []
Entra app count before/after: 2 / 2
```

The group and resources carried `purpose=801-verify owner=opencode-temp`.
No subscription/tenant/provider changes, unrelated resource-group access,
role assignments or Entra objects were made. Sanitized full logs, final
`instanceView`, state history, lifecycle/cleanup and extracted `cells.json` are
retained locally under `/tmp/opencode/evidence-801-final/`. Local paths are not
permanent public artifacts. No invoice or total billable-request claim; timeout
cells and fixture setup add uncounted traffic. Resources were deleted before
analysis/writing. Both issues remain incomplete, with checkboxes unchanged.

## Test structure

```
tests/
├── conftest.py            # Shared fake graphs, fixtures and example-import isolation
├── example_imports.py     # Loads example files without leaking their sibling modules
├── test_app.py            # LangGraphApp core tests
├── test_contracts.py      # Pydantic model tests
├── test_protocols.py      # Protocol interface tests
├── test_public_api.py     # Public API surface tests
├── integration/           # Service-dependent emulator tests (-m integration)
├── host_smoke/            # Function app used by the Functions host smoke
├── host_lanes/            # Platform app and SDK scenarios for the Functions host lanes
└── e2e/                   # Real-Azure tests (-m e2e)
```

The listing is a map, not an inventory; browse `tests/` for the full set.

## Fake graph fixtures

Tests use fake graph implementations instead of real LangGraph graphs. They are defined in `tests/conftest.py` and exposed as the fixtures `fake_graph`, `fake_graph_with_checkpointer`, `fake_invoke_only_graph`, `fake_failing_graph`, `fake_stateful_graph`, `fake_failing_stateful_graph`, `fake_not_found_stateful_graph` and `fake_failing_update_state_graph`. The snippets below are checked against `tests/conftest.py` by `tests/test_testing_docs.py`.

Each fake returns canned values: it does not look at its input, so a passing test shows how the app handles a graph's result, not that a graph computed it.

### FakeCompiledGraph

Satisfies `LangGraphLike` (both `invoke()` and `stream()`). `invoke()` returns the configured `invoke_result` and `stream()` yields the configured `stream_results`, ignoring the input:

```python
class FakeCompiledGraph:
    def __init__(
        self,
        invoke_result: dict[str, Any] | None = None,
        stream_results: list[dict[str, Any]] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self._invoke_result = invoke_result or {
            "messages": [{"role": "assistant", "content": "Hello!"}]
        }
        self._stream_results = stream_results or [
            {"messages": [{"role": "assistant", "content": "chunk1"}]},
            {"messages": [{"role": "assistant", "content": "chunk2"}]},
        ]
        self.checkpointer = checkpointer

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._invoke_result

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        yield from self._stream_results
```

### FakeInvokeOnlyGraph

Satisfies `InvocableGraph` only (no `stream()` method):

```python
class FakeInvokeOnlyGraph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"result": "ok"}
```

### FakeFailingGraph

Raises from both `invoke()` and `stream()`:

```python
class FakeFailingGraph:
    checkpointer = None

    def invoke(self, input: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("Graph execution failed")

    def stream(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
        stream_mode: str = "values",
    ) -> Iterator[dict[str, Any]]:
        raise RuntimeError("Stream execution failed")
```

### Stateful fakes

`FakeStatefulGraph` adds `get_state()`, `update_state()` and `get_state_history()` on top of `FakeCompiledGraph`, backed by a configurable `_FakeStateSnapshot`. `FakeFailingStatefulGraph`, `FakeNotFoundStatefulGraph` and `FakeFailingUpdateStateGraph` cover the error paths of the state routes. Read `tests/conftest.py` for their exact behavior rather than relying on a copy here.

## Writing new tests

### Testing graph behavior

Use a fake fixture to test endpoint behavior (this mirrors `TestInvokeHandler` in `tests/test_app.py`):

```python
def test_my_feature(self, fake_graph: FakeCompiledGraph) -> None:
    app = LangGraphApp()
    app.register(graph=fake_graph, name="test")

    req = self._make_request({"input": {"messages": []}})
    resp = app._handle_invoke(req, app._registrations["test"])

    assert resp.status_code == 200
```

`_make_request` is a small helper defined on the test class that builds a `func.HttpRequest` with a JSON body.

### Testing error cases

```python
def test_invalid_json(self, fake_graph: FakeCompiledGraph) -> None:
    app = LangGraphApp()
    app.register(graph=fake_graph, name="test")

    req = func.HttpRequest(
        method="POST",
        url="/api/graphs/test/invoke",
        body=b"not json",
    )
    resp = app._handle_invoke(req, app._registrations["test"])
    assert resp.status_code == 400
```

### Importing examples safely

An example's `function_app.py` imports its siblings by bare name (`graph`, `model_config`, `tools`), so two examples loaded in one process would otherwise see each other's modules. Load example code through `tests.example_imports.load_example_file()`, which forgets those names first; the autouse fixture in `tests/conftest.py` restores `sys.modules` and `sys.path` after every test. Do not import an example with a plain `import graph` in a test.

## Coverage

Run with HTML coverage report:

```bash
make cov
```

View the report:

```bash
open htmlcov/index.html
```

The `coverage.xml` file is generated for CI integration with Codecov.
