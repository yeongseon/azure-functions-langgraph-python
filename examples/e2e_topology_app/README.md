# Opt-in Azure topology smoke payload

This is a disposable test app, not production guidance. The `certify_topology`
input of `e2e-azure.yml` deploys a wheel built from the selected ref to one Linux
Y1 / Python 3.12 app. Nothing in the publish workflow enables this lane.

The workflow changes only app settings between phases:

| Mode | Declaration | Final wiring | Expected |
| --- | --- | --- | --- |
| `safe` | Both package declarations `distributed` | Blob lock/checkpointer, Table store | Health and echo invoke succeed |
| `lock` | Same | Default process-local lock | No indexed routes |
| `store` | Same | Default process-local Platform store | No indexed routes |
| `checkpointer` | Same | InMemorySaver | No indexed routes |
| `warnings` | Both declarations empty | Local lock/store; shared and local savers | W1–W3, health and echo invoke succeed |

Every refusal is followed by a safe recovery with the same payload. Settings
changes restart the app automatically; an explicit restart is also issued. The
HTTP refusal observation is bounded: after restart and 60 seconds of settling,
four routes must return only 404/503 (transport errors are recorded but are not
sufficient evidence alone) throughout 90 seconds of polling. A 200, including
from stale safe workers in that window, fails the lane. This cannot prove that
no old worker answered during the preceding settings/restart transition, or that
every possible route was probed. Offline tests independently prove rejection
before route export; Azure HTTP evidence alone cannot expose internal indexing.

`GET /api/topology/evidence` is function-key protected and registered only after
`function_app` builds successfully. It reports the same private topology
derivation/evaluator used by the library, actual hosted-marker presence (never
marker values), runtime versions, candidate SHA and findings. It does not
fabricate hosted markers. Warnings are derived on the actual warning-mode app;
this observes rule evaluation, not delivery of Python warnings into Azure logs.

Evidence is uploaded as `azure-topology-smoke`, separate from `azure-cert`:
candidate wheel hash/SHA, per-phase HTTP observations and timing, successful
build plans, no-skip JUnit reports and a best-effort diagnostic-presence result.
Existing logs are downloaded privately, searched only for the guard messages,
and neither raw logs nor Azure credentials are uploaded. Logging/Application
Insights is not enabled; unavailable diagnostic text stays NOT OBSERVED.

Scope limits: one app on one plan/runtime is not multi-instance certification,
checkpoint fencing, warning-only enforcement, all safe configurations, or proof
that the package diagnostic is available to an operator on Azure. No Table CRUD
or Platform parity is claimed. The safe invoke actually writes Blob checkpoints
under a Blob lease; the Table client is wiring inspected by the build guard.
The workflow cleanup owns the temporary resource group, even after failure.

## Manual Flex evidence (2026-11-27 UTC)

One maintainer-authorized deployment for #880 extends the Y1 observations in
[PR #973](https://github.com/yeongseon/azure-functions-langgraph-python/pull/973).
It does not change that opt-in workflow, required CI, release gates, the guard,
constructor signatures or compatibility classification. The app entrypoint,
topology builder, graph and HTTP test were unchanged. This is a single run, not
repeatability or universal certification.

### Candidate and deployment

| Item | Recorded value |
| --- | --- |
| Candidate | `origin/main` at `7776a5bfa1eebe64fda44b73126d0fdfb1fc47e6`, adapter `0.14.2` |
| Wheel SHA256 | `fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3` |
| Build / tools | `uv build --wheel`; uv `0.11.7`, Azure CLI `2.83.0`, Core Tools `4.8.0` |
| Actual runtime | Python `3.12.16`, azure-functions `1.26.0`; Functions v4 |
| Plan / region | Linux Flex Consumption (`FC1`), `koreacentral`, 512 MB per instance |
| Scale configuration | Platform returned `alwaysReady=[{name:http, instanceCount:2}]`, `maximumInstanceCount=40`, `triggers=null` |
| Storage | Standard_LRS; connection strings for host, deployment, Blob checkpoint/lease and Table wiring; no identity-only experiment |
| Telemetry | Own PerGB2018 Log Analytics workspace (30-day retention) and workspace-linked Application Insights component inside the same temporary group |
| Lifetime | Provisioning began `2026-11-27T23:31:37Z`; deletion verified `2026-11-27T23:50:23Z` (under 19 minutes) |

The wheel was copied to `wheels/` in a private copy of this example. Its
`requirements.txt` appended
`./wheels/azure_functions_langgraph-0.14.2-py3-none-any.whl[azure-blob,azure-table]`.
Core Tools published that copy with
`func azure functionapp publish <APP> --python --build remote`; publishing
succeeded. The protected evidence route reported the candidate SHA and versions
above, so the result is tied to the bundled candidate, not the published PyPI
package with the same version number.

Provisioning reused `infra/flex.bicep` with `identityBasedStorage=false`,
`pythonVersion=3.12`, `instanceMemoryMB=512`, `maximumInstanceCount=40` and unique
app/storage names. The existing template creates no Application Insights; no
`az functionapp create` was used. A workspace and component were created
explicitly in the owned group, so no default workspace/group was needed.
Application Insights was connected through an environment-held connection
string. Host log level was `Information` and host telemetry sampling was
disabled for this bounded diagnostic experiment. No keys, connection strings,
tokens or Azure identity IDs were saved as evidence.

Python 3.12 was selected because the unchanged test explicitly asserts 3.12 and
the previous Y1 run used it, isolating the plan change. Python 3.13/3.14, the
2.x Functions worker and keyless host settings were **not tested**. Always-ready
`http=2` was configured with `az functionapp scale config always-ready set`.
The template's on-demand maximum remained 40; no load generator or concurrency
override was used. The short run used the smallest supported instance size and
made no model calls, latency claim or price measurement.

### Eight HTTP phases

The existing `tests/e2e_topology/test_topology_azure.py` ran once per phase with
`-m e2e_topology -o addopts=''`, followed by `tools/check_junit.py` for each
report: **8 passed, zero skips, failures or errors**. The test runner was Python
3.11.15 / pytest 9.2.0; deployed runtime versions came from the app itself.

| Phase | Observable result | Probe duration / seconds since settings change |
| --- | --- | --- |
| `safe` | Health/evidence 200; keyed native invoke 200 with `output.message=topology-smoke`; no findings | 1.5 / 68.5 |
| `lock` | All four routes 404, 30 observations per route (120 total) | 91.2 / 164.0 |
| `recovered-lock` | Safe health/evidence/invoke assertions passed; no findings | 1.5 / 67.9 |
| `store` | All four routes 404, 30 observations per route (120 total) | 91.0 / 157.2 |
| `recovered-store` | Safe health/evidence/invoke assertions passed; no findings | 1.6 / 68.6 |
| `checkpointer` | All four routes 404, 30 observations per route (120 total) | 91.1 / 157.9 |
| `recovered-checkpointer` | Safe health/evidence/invoke assertions passed; no findings | 1.4 / 67.6 |
| `warnings` | Health/evidence/invoke passed; exactly W1–W3 at warning severity | 1.7 / 68.3 |

Four refusal probes were `GET /api/health`, `GET /api/topology/evidence`,
`POST /api/graphs/agent/invoke` and `POST /api/threads`. Across all three refusal
windows there were **360 HTTP 404 responses**, no 200, 503 or transport errors.
Each window began after settings update, explicit restart and 60 seconds of
settling, then sampled for at least 90 seconds. The preceding transition was
not continuously observed. Safe invoke uses a fresh thread ID and actually
writes Blob checkpoints under a Blob lease; safe Table CRUD was not exercised.
Successful invoke status/output assertions execute separately from the test's
status arrays, which are empty for invoke in safe-phase JSON.

The evidence route returned `WEBSITE_SITE_NAME=true`,
`WEBSITE_INSTANCE_ID=false`, `azure_hosted=true`. In warning mode, it returned
`thread-store-hosted`, `process-local-checkpointer-hosted` and
`inprocess-lock-hosted`, all warning severity. This proves evaluator output on
the actual hosted app, **not warning delivery into platform logs**.

The existing test writes a fixed `scope` string naming Y1 into its JSON reports.
That string is not deployment discovery and does not describe this run: the
actual plan was FC1 from `infra/flex.bicep`. It was left unchanged rather than
editing the harness to make the evidence appear broader.

### Diagnostic and multi-instance findings

**Azure diagnostic delivery: NOT OBSERVED.** The owned Application Insights
query over `traces` and `exceptions` attempted aggregate counts of indexing
failures and the three package refusal messages. Azure CLI returned
`BadArgumentError: The request had some invalid properties`. No diagnostic
result was obtained; the cause of that query error was not established.
There was no raw log download, alternative permission path or redeployment.
Creating and connecting telemetry is not proof that a message was ingested or
retrievable. The subsequent per-route request/instance query was not reached
because the runner exited on the first query failure.

**Actual multi-instance behavior: NOT PROVEN.** The platform accepted and
returned two always-ready HTTP instances in configuration, but configuration is
not observed routing. `boot_id` identifies an app build, not an Azure instance,
and the hosted-marker booleans do not supply instance count. No distinct live
instance IDs, simultaneous requests on two instances, per-instance refusal
diagnostics or platform request-instance counts were collected. These remain
missing evidence; the run must not be described as multi-instance certification.

| Issue acceptance / remaining scope | Evidence status |
| --- | --- |
| Unsafe combinations fail with actionable errors before first request | Three Flex refusal windows observed; local host/unit tests establish pre-export ordering and actionable text. Azure internal indexing exception/text and the pre-window transition remain unobserved |
| Safe deployments and constructor signatures retain behavior | Same candidate's safe baseline and all three recoveries passed; no source/API change. Other safe configurations were not exhaustively exercised |
| Deterministic tests plus scoped Azure smoke | Existing test/JUnit validator passed all eight phases on Flex, supplementing the earlier Y1 run; no new required Azure lane |
| Diagnostic delivery and multi-instance observation | Incomplete: telemetry query error and configured-but-unobserved instance count; maintainer compatibility classification remains a separate decision |

No exactly-once behavior, cross-instance exclusion, checkpoint fencing, every
route, all plans/runtimes or release certification is inferred. Issue #880
remains a reference, not a completion request; no checkboxes were changed.

### Cleanup before documentation

An exit trap deleted only this run's uniquely named
`rg-aflg-880-verify-<RANDOM>` group and awaited deletion, including on the
telemetry-query failure. It contained the app, FC1 plan, Standard_LRS storage,
workspace and Application Insights component. No Entra objects or role
assignments were created, and no unrelated group was modified or deployed into.
Documentation was written only after the following checks completed:

```text
az group exists -n <OWNED_RG>: false
az resource list --tag purpose=880-verify --query '[].name': []
DefaultResourceGroup-* before: []
DefaultResourceGroup-* after: []
cleanup_utc=2026-11-27T23:50:23Z
```
