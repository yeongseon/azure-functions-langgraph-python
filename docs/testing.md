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

### Real-Azure Blob comparison — 2026-11-24 (partial evidence for #860)

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
