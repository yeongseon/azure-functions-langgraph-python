# Flex Consumption + Blob production reference

## Scope and limits first

This is the single native HTTP reference selected in [#882](https://github.com/yeongseon/azure-functions-langgraph-python/issues/882#issuecomment-7145048572):
**Flex Consumption + Azure Blob checkpoints + Azure Blob lease lock, system-assigned
Managed Identity, no storage keys in app settings**. Table storage is only for the
optional Platform surface and is not provisioned here. Postgres is deferred.

**Evidence status: one all-green live run on one deployment; not a release certification and not a
repeatability claim.** On 2026-11-24 UTC the commands of this page were run in order from a fresh
checkout of commit `951e616` (the deviations are listed under "Dated Azure evidence"): the remote
build installed the published 0.14.2 wheel and the isolated harness reported **5 passed, 0 failed,
0 skipped** in 37.88 s. It observed anonymous liveness 200, keyless invoke 401 and keyed invoke 200,
the installed versions, same-thread persistence and cross-thread isolation, four concurrent
same-thread requests (409, 200, 200, 409) and persistence across an Azure restart. Four earlier
cycles stopped on defects in the verification scaffolding and in the example's health
configuration, not on the package; they are kept below for the record. One run, one region, one
app: do not generalize it.

Verified deployment: Korea Central, Linux FC1 Flex Consumption, Python 3.11,
2048 MB, no always-ready instances; published adapter wheel 0.14.2; public HTTPS
ingress and public storage endpoints with anonymous Blob access disabled.
Only short deterministic fake-model requests were exercised.

**Untested:** other regions, plans (including Premium and Y1), Python versions,
azure-functions 2.x in this reference, Postgres/Cosmos, VNet/private endpoints,
multi-instance scale-out, Platform/Table, live Azure OpenAI, genuine cold starts,
load/soak, restart during active work, injected lease loss in this deployment,
and long-running workflows. A configured maximum of 40 is not evidence that
multiple instances ran. No production capacity or exactly-once claim is made.

**Critical safety boundary:** a Blob lease protects a lock marker, not checkpoint
writes after lease loss. The opt-in `guard_checkpoint_writes` only reduces writes
after the lock **detects** loss; it is not fencing and cannot stop graph side
effects. This example leaves that option off. See the [lease safety and recovery
procedure](production-guide.md#blob-lease-safety-and-recovery) and [guard contract](production-guide.md#opt-in-guard-for-blob-checkpoint-writes-790).
PR [#970](https://github.com/yeongseon/azure-functions-langgraph-python/pull/970)
observed stale writes after a real lease break, and guarded rejection only after
detection; those are separate storage-client probes, not this Flex run.
Function keys grant application access, **not user/thread ownership**. Add your
own authenticated principal and thread authorization before exposing real data.

## Decision record

- Keep `infra/flex.bicep` as the only topology definition. No azd/`azure.yaml`:
  another wrapper has not demonstrated better reproducibility without duplication.
- Flex matches the tutorial and existing template. Microsoft documents [Linux
  Consumption retirement on 2028-09-30](https://learn.microsoft.com/azure/azure-functions/functions-scale).
- Reuse `examples/production_persistent_agent` unchanged graph/model logic with
  explicit `AGENT_MODEL_MODE=fake`. No cloud model credentials or model charges.
- Blob is the [first inexpensive distributed option](hardening-status.md).
  Postgres would add unverified networking, credentials, pooling and cost choices.
- The local demo stays separate; this does not restructure the learning examples.

## Prerequisites

Bash, git, Python 3.11, Azure CLI with Bicep, Functions Core Tools v4, a logged-in
Azure subscription supporting Flex in Korea Central, and permission to create
resources and app-identity role assignments **inside your dedicated group**.
Verification uses pytest, requests and Pydantic from the repository dev environment.
The recorded tools were Azure CLI 2.83.0 and Core Tools 4.8.0. Use a matching local
Python 3.11 on PATH; Core Tools otherwise warns even though remote build uses the
configured Azure interpreter. Review spend and RBAC before running. Do not enable
shell tracing or print keys, full app settings, resource IDs or raw HTTP bodies.

## Deploy from a clean checkout

Run the following in one dedicated Bash 4 or newer shell (errors exit it; the block
uses `${var,,}`, which macOS's default Bash 3.2 and zsh do not support). Set `SUBSCRIPTION`
to your intended subscription name or ID before pasting. Choose globally unique app
and storage names. These commands
use a **published wheel**, not editable source. The separate reference requirements
do not change the ordinary example dependency bounds.

```bash
set -euo pipefail
(( ${BASH_VERSINFO[0]:-0} >= 4 )) || { echo 'This block requires Bash 4 or newer.' >&2; exit 1; }
: "${SUBSCRIPTION:?Set SUBSCRIPTION to the intended subscription name or ID}"
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python
# For an exactly repeatable run, check out the release tag or commit you intend to verify.
make install
SUBSCRIPTION_ID=$(az account show --subscription "$SUBSCRIPTION" --query id -o tsv)
test -n "$SUBSCRIPTION_ID"
EXECUTION=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
RG="rg-af-ref-$EXECUTION"
readonly SUBSCRIPTION_ID EXECUTION RG
export RG
az account set --subscription "$SUBSCRIPTION_ID"
export APP="af-ref-$(date -u +%m%d%H%M)-$RANDOM"
export ST="stafref$(date -u +%m%d%H%M)$RANDOM"
export LOCATION=koreacentral
verify_owned_group() {
  local actual_id actual_tag expected_id
  expected_id="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RG"
  actual_id=$(az group show --subscription "$SUBSCRIPTION_ID" --name "$RG" --query id -o tsv) || return 1
  actual_tag=$(az group show --subscription "$SUBSCRIPTION_ID" --name "$RG" --query tags.execution -o tsv) || return 1
  if [[ "${actual_id,,}" != "${expected_id,,}" || "$actual_tag" != "$EXECUTION" ]]; then
    echo 'Refusing resource group operation: subscription/group or execution tag differs.' >&2
    return 1
  fi
}
cleanup() {
  if ! verify_owned_group; then
    echo 'Cleanup refused: ownership verification failed; inspect the group manually.' >&2
    return 1
  fi
  az group delete --subscription "$SUBSCRIPTION_ID" --name "$RG" --yes --output none || {
    echo 'Cleanup failed: resource group delete failed.' >&2; return 1;
  }
  az group wait --subscription "$SUBSCRIPTION_ID" --name "$RG" --deleted || {
    echo 'Cleanup failed: resource group deletion wait failed.' >&2; return 1;
  }
}
az account show --subscription "$SUBSCRIPTION_ID" --query '{name:name,state:state}' -o json
az bicep build --file infra/flex.bicep --outfile /tmp/flex-reference.json
group_exists=$(az group exists --subscription "$SUBSCRIPTION_ID" --name "$RG" -o tsv)
if [[ "$group_exists" != false ]]; then
  echo 'Refusing to create or clean up a resource group that already exists or could not be checked.' >&2
  exit 1
fi
az group create --subscription "$SUBSCRIPTION_ID" --name "$RG" --location "$LOCATION" --tags \
  purpose=production-reference repo=azure-functions-langgraph issue=882 owner=agent execution="$EXECUTION" -o none
verify_owned_group
trap 'status=$?; trap - EXIT; cleanup || exit 1; exit "$status"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
az deployment group validate --subscription "$SUBSCRIPTION_ID" --resource-group "$RG" --template-file infra/flex.bicep \
  --parameters functionAppName="$APP" storageName="$ST" location="$LOCATION" \
  pythonVersion=3.11 identityBasedStorage=true -o none
az deployment group create --subscription "$SUBSCRIPTION_ID" --resource-group "$RG" --template-file infra/flex.bicep \
  --parameters functionAppName="$APP" storageName="$ST" location="$LOCATION" \
  pythonVersion=3.11 identityBasedStorage=true -o none
ROOT="$PWD"
PAYLOAD=$(mktemp -d)
cp -a examples/production_persistent_agent/. "$PAYLOAD/"
cp "$PAYLOAD/requirements-reference.txt" "$PAYLOAD/requirements.txt"
(cd "$PAYLOAD" && func azure functionapp publish "$APP" --python --build remote)
cd "$ROOT"
```

The random execution ID avoids minute-based group collisions. Cleanup is armed
only after create succeeds and the group's resource ID (including subscription)
and execution tag match; it rechecks both immediately before deletion. A failed
create or initial ownership check leaves cleanup unarmed: inspect any partial
group manually. Errors remain on stderr and produce a failing exit status. Keep
this shell's subscription selected for Core Tools and the later harness; group
operations below are explicitly scoped as well. Tags are an accidental-deletion
guard, not an authorization boundary or atomic protection against another writer
replacing a group between verification and deletion. Never share this group.
The deployment block is exercised offline by
`tests/test_production_reference_cleanup.py` with a fake Azure CLI. These cleanup
changes postdate the recorded 2026-11-24 run and have **not** been run live; the
dated evidence below describes the earlier command sequence, not this revision.

The `.funcignore` excludes local environments, settings and caches; the observed
source upload was 13.92 KB, not a local venv. Keep the payload free of secrets.
Identity roles are created after the Function App identity exists. Allow RBAC
propagation before diagnosing storage failures; do not fall back to storage keys.
The first cycle's `az functionapp show` returned null Flex properties: use the
ARM query below, not that null as evidence of absent runtime settings.

```bash
az resource show --subscription "$SUBSCRIPTION_ID" --resource-group "$RG" --name "$APP" \
  --resource-type Microsoft.Web/sites --api-version 2024-04-01 \
  --query '{runtime:properties.functionAppConfig.runtime,scale:properties.functionAppConfig.scaleAndConcurrency,auth:properties.functionAppConfig.deployment.storage.authentication.type}'
az storage account show --subscription "$SUBSCRIPTION_ID" -g "$RG" -n "$ST" \
  --query '{sharedKey:allowSharedKeyAccess,publicBlob:allowBlobPublicAccess,tls:minimumTlsVersion}'
az functionapp config appsettings list --subscription "$SUBSCRIPTION_ID" -g "$RG" -n "$APP" --query '[].name'
```

## Run the isolated verification harness

```bash
export REF882_BASE_URL="https://$APP.azurewebsites.net"
export REF882_RESOURCE_GROUP="$RG" REF882_APP="$APP"
export REF882_EVIDENCE="$(mktemp)"
REF882_FUNCTION_KEY="$(az functionapp keys list -g "$RG" -n "$APP" \
  --query functionKeys.default -o tsv)" \
hatch run python -m pytest tests/e2e_reference -m e2e_reference \
  -o addopts='' --tb=no --show-capture=no -q
```

The harness checks anonymous liveness, rejected keyless and successful keyed
invoke, explicit keyless denial on `/api/reference/runtime` alongside authenticated
installed-distribution versions, two-request accumulated state
through the state endpoint, cross-thread isolation, four concurrent same-thread
requests and persistence across `az functionapp restart`. The runtime endpoint
`GET /api/reference/runtime` is function-key protected and returns versions plus
an opaque random token created once per worker-process import. Bare PIDs are
insufficient: separate containers can reuse the same PID. The token is compared
in memory only, excluded from evidence serialization and never printed.
Run only against an expendable app: the restart is disruptive.

The runtime keyless-denial assertion is covered offline with fake HTTP responses;
the dated run below predates that assertion. A new live run is needed to establish
its result on a deployment. Only status codes and validated versions are recorded.

Restart verification separates **restart-command acceptance**, **HTTP availability**
and **confirmed process turnover** into boolean evidence fields. For correlation,
the same protected runtime route accepts `restart_thread=restart-<32 hex digits>`:
POST writes the fixed fake-model `before` turn and reads its checkpoint; GET reads
that checkpoint. Both return the process token from the process performing the
operation, with `Cache-Control: no-store`. These probes reject non-reference thread
names and any model mode other than explicit `fake`; they use the existing Blob
checkpointer and add no dependency or change to function-key authentication.
The probe thread is unique to this run and must have no concurrent writers.

After the restart command returns success, the harness polls authenticated GET
probes for up to 180 seconds. An old worker returning 200 proves availability only:
its unchanged token cannot pass recovery. A changed token must accompany the
actual state read containing the pre-restart turn before the normal native append
and state assertions run. Timeout records turnover as unverified and fails, even
when HTTP stayed available. This proves an observed different serving process,
not the retirement of every old worker, scale-out, or recovery during active work.

The dated 2026-11-24 restart observations below establish command acceptance and
HTTP/state availability only; process turnover was **unverified**. The stronger
correlated check has offline fake-HTTP/CLI coverage only and awaits an authorized
live re-run on an expendable app. Historical timings and outcomes are unchanged.

Concurrent invokes may return **200 or 409**. Native invoke calls
`thread_lock.acquire(graph_name, thread_id)` without a timeout; the Blob lease
backend defaults to `timeout=0.0` and does not poll on contention. A locally
tracked owner or an Azure lease conflict returns no token, which native invoke
maps to 409. Storage calls still take time; zero lock wait is not zero storage-call
latency. Multiple requests can succeed when their acquisition attempts occur
after earlier owners release: concurrent HTTP requests do not establish
overlapping graph execution. The harness requires at least one success, not
exactly one winner. Persisted state must contain every accepted
turn exactly once and its matching assistant turn, and no rejected input. A later
append must work. This is not a fairness or cross-instance scale-out assertion.

Missing base URL/key skips the live tests; missing group/app skips restart. A
verification run requires **all five tests passing, zero skips**. Evidence JSON
contains allow-listed status/count/timing tags, restart booleans and numeric versions,
not keys, URLs, bodies, process tokens or resource IDs.
Use `--tb=no --show-capture=no`; never `--showlocals`.
This directory is outside `tests/e2e/`: release collection stays at its original
12 tests. Marker `e2e_reference` is registered alongside `e2e`; no workflow is
changed. A re-certification CI lane is follow-up work, not part of this PR.

## Settings and RBAC

`identityBasedStorage=false` remains the template default: the existing 2.x lane
keeps its connection-string behavior and parameters. Opt in explicitly here.

| Setting | Reference value / purpose |
| --- | --- |
| `AzureWebJobsStorage__accountName` | Created account; identity-based host storage |
| `AzureWebJobsStorage__credential` | `managedidentity`; system identity, no client ID |
| Deployment authentication (site configuration) | `SystemAssignedIdentity`, not an app-setting connection string |
| `AZURE_STORAGE_BLOB_ACCOUNT_URL` | Created account Blob URL; selects MI with no secret fallback |
| `LANGGRAPH_BLOB_CONTAINER` | `langgraph-checkpoints`; pre-created checkpoints plus `thread-locks/` markers |
| `AGENT_MODEL_MODE` | `fake`; explicit deterministic model |
| `LANGGRAPH_ENABLE_PLATFORM` | `false`; no Table store |
| `LANGGRAPH_ENABLE_DISTRIBUTED_LOCK` | `true` |
| `LANGGRAPH_AUTO_CREATE_STORAGE` | `false`; no cold-start provisioning |
| `AZFUNC_LANGGRAPH_LOCK_BACKEND` | `distributed`; startup rejects accidental local default |
| `LANGGRAPH_STRICT_MSGPACK` | `true`; restrict checkpoint deserialization |

Do not set `AzureWebJobsStorage`, `DEPLOYMENT_STORAGE_CONNECTION_STRING` or
`AZURE_STORAGE_CONNECTION_STRING` in this identity reference.

| Actual role | Scope | Principal | Why |
| --- | --- | --- | --- |
| Storage Blob Data Owner (`b7e6dc6d-f1e8-4753-8033-0f276bb0955b`) | The one storage account, inside the dedicated group | Function App's own system-assigned identity | Microsoft host-storage minimum; host creates/manages its own Blob containers. Includes deployment package read/write and application checkpoint/lease operations. |

Microsoft's [identity connections guidance](https://learn.microsoft.com/azure/azure-functions/manage-connections)
requires Blob Data Owner for host storage. [Flex deployment settings](https://learn.microsoft.com/azure/azure-functions/flex-consumption-how-to#configure-deployment-settings)
require Blob Data Contributor for deployment storage. Owner already includes those
permissions on this shared account, so assigning Contributor at deployment or
checkpoint container scope adds no isolation or privilege reduction. Account scope
is the narrowest documented host scope; separate accounts would narrow application
isolation but violate this reference's one-account budget/topology. There are no
user, subscription or resource-group role grants, Queue roles, custom roles or
service principals created separately. A managed identity has an Entra service
principal internally; it is not a separately provisioned credentialed principal.

No Table role is assigned: host diagnostic Table persistence and Platform/Table
are not claimed. If you require those diagnostics, Microsoft documents an
additional Storage Table Data Contributor role; assess it separately rather than
silently expanding this reference. The optional [MI Blob/Table example](../examples/managed_identity_storage/)
requires both endpoints and rejects partial MI configuration; it is not certified here.

[Microsoft IaC guidance](https://learn.microsoft.com/azure/azure-functions/functions-infrastructure-as-code)
supports disabling Shared Key for identity-based Flex. This mode sets
`allowSharedKeyAccess=false`, TLS 1.2 minimum, anonymous Blob access false, and
HTTPS-only ingress. It does **not** create a firewall, VNet, private endpoint,
Application Insights, database, Key Vault or always-ready instance. Public network
reachability is deliberate and untested private networking must not be inferred.

## Failure modes and operations

| Signal | Meaning / action |
| --- | --- |
| 401 on invoke | Missing/invalid function key; use header, never log it |
| 401 on liveness | Wrong example health configuration; liveness must be anonymous while details remain protected |
| 409 on invoke | Same-thread lock contention: native invoke uses the Blob backend's zero-wait default, without contention polling; use bounded client backoff, no blind replay of uncertain side effects |
| 500 on invoke/state | Storage permission/container failure or graph error; inspect redacted host diagnostics, do not infer 404 |
| Storage 403 | Check identity role, scope and propagation; never enable Shared Key as a workaround |
| Lease loss warning | Pause conflicting work, reconcile checkpoints/side effects; [recovery procedure](production-guide.md#blob-lease-safety-and-recovery) |
| Restart | Completed checkpoint state survived in this run; active work/cancellation not tested |

Bound model/tool/storage timeouts, retain/prune checkpoints deliberately, monitor
lease-renewal warnings, and drain old writers during deployments. No Application
Insights or production alerting configuration is certified. See the existing
[production guide](production-guide.md#distributed-deployment) for operational
limits; buffered SSE and HTTP response deadlines do not disappear on Flex.

## Dated Azure evidence — 2026-11-24 UTC

### Final run: the documented commands, all five live tests passing

Commit `951e616`, 22:33 to 22:39 UTC. The commands above were executed in order as one script
with an `EXIT` cleanup trap. Deviations, all forced by the workstation: a `python` shim on
`PATH` pointing at the local Python 3.11 (see Prerequisites); the existing Hatch environment's
Python in place of `hatch run`, because Hatch environment creation hangs offline there; and
`make install` skipped for the same reason. The remote build installed the **published 0.14.2
wheel**. The protected runtime endpoint, not the workstation venv, reported the package versions
below; the Functions host version comes from the publish output.

| Runtime item | Observed resolved version (final run only) |
| --- | --- |
| Python | 3.11.18 |
| azure-functions-langgraph | 0.14.2 |
| azure-functions | 1.26.0 |
| langgraph | 1.2.24 |
| langchain-core | 1.8.4 |
| azure-storage-blob | 12.31.1 |
| azure-identity | 1.27.0 |
| Functions host (publish output) | 4.1055.300 |

These are the resolved versions reported by that deployment, not a complete
dependency snapshot or an exact-replay specification. The final run's sanitized
runtime inventory recorded Python and the six distributions above; it is not a
full inventory of transitive dependencies. The host version is separately
sourced from the publish output, not the runtime endpoint.

A **new deployment is a new verification run**, not a reproduction certified by
this result. `requirements-reference.txt` fixes the adapter wheel but allows
`azure-functions>=1.21,<2`, `langgraph>=1.0,<2` and `langchain-core>=1.0,<2`;
the Blob/identity extras and transitive dependencies are not fully pinned there.
Package-index contents and dependency resolution may therefore produce different
installed versions even from the same source checkout. A chosen source revision,
local Python/CLI/Core Tools versions, remote build environment, Azure-managed
Python patch and Functions host versions, deployment names, region/parameters,
identity/RBAC propagation and request timing are also inputs to record for the
new run, rather than values established for future runs by this evidence.
Selecting a source commit alone cannot freeze those inputs. No exact dependency
replay has been verified or is offered here; capture the new runtime inventory
and evaluate the harness results and limitations for that deployment separately.

| Check / operation | Actual observation |
| --- | --- |
| Bicep build / group create / ARM validate / provision | 5 s / 2 s / 5 s / 98 s, every exit code 0 |
| Template as deployed | Linux FC1 Flex Consumption, Python 3.11, 2048 MB, maximum 40, no always-ready instances; system-assigned identity; deployment storage authentication `SystemAssignedIdentity` |
| Shared Key / anonymous Blob access / TLS | false / false / TLS1_2 |
| App setting names | `AzureWebJobsStorage__accountName`, `AzureWebJobsStorage__credential`, `AZURE_STORAGE_BLOB_ACCOUNT_URL`, `LANGGRAPH_BLOB_CONTAINER`, `AGENT_MODEL_MODE`, `LANGGRAPH_ENABLE_PLATFORM`, `LANGGRAPH_ENABLE_DISTRIBUTED_LOCK`, `LANGGRAPH_AUTO_CREATE_STORAGE`, `AZFUNC_LANGGRAPH_LOCK_BACKEND`, `LANGGRAPH_STRICT_MSGPACK`; no connection-string setting |
| Role assignments listed for the storage account | Exactly one: Storage Blob Data Owner, principal type ServicePrincipal (the app's system-assigned identity). The compiled template defines its scope as the storage account. The helper's string comparison of the listed scope with the account id returned false, which is consistent with resource-ID casing differences but was not re-checked because the group had already been deleted |
| Remote wheel build and publish | 113 s, exit 0 |
| Auth | 200 / 401 / 200; 5.684 s, not a cold-start measurement |
| Installed runtime versions | the table above |
| Same-thread persistence and cross-thread isolation | passed; 19.657 s |
| Four concurrent same-thread invokes | 409 / 200 / 200 / 409; 4.936 s; the harness asserts that both accepted turns persisted exactly once, no rejected input appears, and a later append works |
| Restart persistence | passed; 2.546 s |
| Live pytest aggregate | **5 passed / 0 failed / 0 skipped**, 37.88 s, exit 0 |
| Cleanup | group deleted and waited on; `exists` false after 78 s; no owned `rg-af-ref-*` group remains |

### Earlier cycles (one to four)

Application/IaC revision `7f2e7d3`; harness live revision before the subsequent
offline message-envelope correction. Remote build installed the **published
0.14.2 wheel** via reference requirements. The protected runtime endpoint, not
the workstation venv, reported:

| Runtime item | Deployed version |
| --- | --- |
| Python | 3.11.18 |
| azure-functions-langgraph | 0.14.2 |
| azure-functions | 1.26.0 |
| langgraph | 1.2.24 |
| langchain-core | 1.8.4 |
| azure-storage-blob | 12.31.1 |
| azure-identity | 1.27.0 |

| Check / operation | Actual observation |
| --- | --- |
| ARM validate / provision | Passed; 5.701 / 67.480 s |
| Remote wheel build/publish | Passed; 106.139 s; 13.92 KB source upload |
| Shared Key / anonymous Blob access | Both false; TLS1_2; system identity for deployment; no connection-string setting names |
| Auth | Anonymous health 200, keyless invoke 401, keyed invoke 200; combined 5.387 s, not cold start |
| Auth test extra parser | Failed on missing direct message `content`; invoke uses `data.content`; corrected offline, then exercised live in the final run above |
| Installed runtime versions | Passed, table above |
| Same-thread persistence and cross-thread isolation | Passed; 3 invokes plus state reads; 21.728 s |
| Four concurrent invokes | 409 / 200 / 409 / 200; 0.644 s; both accepted turns persisted, rejected absent; later append passed |
| Restart persistence | Passed; state before restart preserved and next turn accumulated; 7.212 s including Azure restart/readiness/state/invoke |
| Live pytest aggregate (cycle three) | **4 passed / 1 failed / 0 skipped**, 35.77 s; superseded by the final run above |

The first three provision cycles: first stopped on evidence wrapper's null CLI
properties; second exposed example's protected-liveness misconfiguration; third
produced the observations above and found the harness envelope mismatch. None
established a package defect. Final app/IaC was not altered after cycle 3.

**Authorized cycle four (2026-11-24 UTC):** restored source and corrected harness
at `d6605ae2647571fcf281a8ed5488d31aa50ea3f8`; merging main was already up to
date. Focused reference/helper/docs/example/Flex tests passed **27/27**; Bicep,
ruff check/format, mypy (316 files), and all three workflow linters passed using
the existing environment fallback. ARM validation/provisioning passed in
5.744/67.937 s. ARM reported Python 3.11, 2048 MB, max 40, no always-ready
instances, HTTPS-only and system-assigned deployment authentication. Storage
Shared Key and public Blob access were both false, TLS1_2; connection-string
setting names were absent.

The temporary wrapper then ran `az role assignment list --scope <owned-account>
--all` and received exit 1. Its stderr was not retained, so the exact CLI cause
is **undetermined**; do not infer an authorization failure. The wrapper raised
`CalledProcessError` before publishing or pytest. This is a wrapper/evidence
collection failure, not an established package/IaC defect. The exception included
a subscription identifier in tool output: a logging-boundary failure, not a
credential leak; it is not reproduced here. Actual role principal/scope proof
was not captured. The template's configured role table above is not a substitute
for that missing live inventory.

| Cycle-four live test | Result |
| --- | --- |
| Liveness/keyless/keyed invoke | Not run |
| Protected runtime versions | Not run; no published code in this cycle |
| Persistence/isolation | Not run |
| Same-thread contention | Not run |
| Restart persistence | Not run |

A fifth cycle followed (the final run above). It exercised the corrected invoke
parser live and listed the role assignments with a simpler command (no `--all`).

| Created group | Contents | Deletion evidence |
| --- | --- | --- |
| `rg-af-ref-202611242144` | FC1 plan, Function App/system identity, Standard_LRS account, Blob containers, one account role | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |
| `rg-af-ref-202611242148` | Same topology, published app | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |
| `rg-af-ref-202611242157` | Same topology, corrected example | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |
| `rg-af-ref-202611242222` | Same provisioned topology; stopped before publish | EXIT delete/wait; exists false; remaining `rg-af-*` list empty |
| `rg-af-ref-202611242233` | Same topology, the documented commands, corrected harness | EXIT delete/wait; `exists` false; no owned `rg-af-ref-*` name remains |

Resource group names above identify disposable evidence resources, not reusable
deployment names. Every group lived for minutes, below the three-hour limit.
Local sanitized logs, evidence JSON, JUnit and command durations are under
`/tmp/opencode/ref882/` and `/tmp/opencode/ref882c/` (the final run); those workstation
directories are **not durable CI artifacts**.
The essential evidence is recorded here; no credentials/subscription IDs are included.

## Cost, cold starts and teardown

Estimated marginal cost for all five short-lived cycles: **under US$1 total**,
not an invoice or measured regional tariff. One Standard_LRS account and FC1 with
no always-ready capacity, tiny deterministic traffic, no model service or telemetry
resource. Actual billing was not measured; no sustained-load estimate is claimed.
Use the [Azure calculator](https://azure.microsoft.com/pricing/calculator/) for
your region/workload. [Flex billing](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan#billing)
distinguishes on-demand execution from always-ready baseline charges.
No genuine cold start was measured: publisher probes warmed the host. Microsoft
documents improved [Flex cold-start behavior](https://learn.microsoft.com/azure/azure-functions/functions-scale#cold-start-behavior),
not a latency promise for this app. Timings above are single observations, not benchmarks.

Delete the group **before** lengthy analysis or documentation work. In the shell
above, exiting runs cleanup; to do it explicitly:

```bash
cleanup
trap - EXIT
az group exists --subscription "$SUBSCRIPTION_ID" --name "$RG" -o tsv
az group list --subscription "$SUBSCRIPTION_ID" --query "[?starts_with(name,'rg-af-ref-')].name" -o tsv
rm -rf "$PAYLOAD"
```

Confirm `exists=false` for your group and that your owned name is absent from the
list; never delete another person's group. The group deletion removes the plan,
app/system identity, account/data/containers and scoped role assignment. Check
Cost Management later for actual charges. Maintainer follow-up: authorize final
SHA/RBAC and decide whether a separate re-certification workflow lane is wanted
(the harness on this page can be driven from one); do not close #882 automatically.
