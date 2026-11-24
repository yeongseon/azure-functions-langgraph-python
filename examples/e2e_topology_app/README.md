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
