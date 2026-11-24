# Flex Consumption + Blob production reference

## Scope and limits first

This is the single native HTTP reference selected in [#882](https://github.com/yeongseon/azure-functions-langgraph-python/issues/882#issuecomment-7145048572):
**Flex Consumption + Azure Blob checkpoints + Azure Blob lease lock, system-assigned
Managed Identity, no storage keys in app settings**. Table storage is only for the
optional Platform surface and is not provisioned here. Postgres is deferred.

**Evidence status: partial, not an all-green certification.** On 2026-11-24 UTC,
one deployed reference passed persistence, isolation, contention and restart tests,
and observed the expected 200/401/200 auth statuses. The five-test live run ended
**4 passed, 1 failed, 0 skipped** because an additional invoke-response assertion
expected the wrong LangChain message envelope. That parser is corrected and tested
offline, but was **not rerun on Azure**: the authorized three-cycle budget was
exhausted. Do not equate this evidence with release certification.

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

Run the following in one Bash shell. Choose globally unique names. These commands
use a **published wheel**, not editable source. The separate reference requirements
do not change the ordinary example dependency bounds.

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python
# Until merged, check out the reference PR branch explicitly.
git checkout docs/flex-blob-production-reference-882
make install
export RG="rg-af-ref-$(date -u +%Y%m%d%H%M)"
export APP="af-ref-$(date -u +%m%d%H%M)-$RANDOM"
export ST="stafref$(date -u +%m%d%H%M)$RANDOM"
export LOCATION=koreacentral
cleanup() {
  az group delete --name "$RG" --yes --output none
  az group wait --name "$RG" --deleted
  az group exists --name "$RG" --output tsv  # must be false
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
az account show --query '{name:name,state:state}' -o json
az bicep build --file infra/flex.bicep --outfile /tmp/flex-reference.json
az group create --name "$RG" --location "$LOCATION" --tags \
  purpose=production-reference repo=azure-functions-langgraph issue=882 owner=agent -o none
az deployment group validate --resource-group "$RG" --template-file infra/flex.bicep \
  --parameters functionAppName="$APP" storageName="$ST" location="$LOCATION" \
  pythonVersion=3.11 identityBasedStorage=true -o none
az deployment group create --resource-group "$RG" --template-file infra/flex.bicep \
  --parameters functionAppName="$APP" storageName="$ST" location="$LOCATION" \
  pythonVersion=3.11 identityBasedStorage=true -o none
ROOT="$PWD"
PAYLOAD=$(mktemp -d)
cp -a examples/production_persistent_agent/. "$PAYLOAD/"
cp "$PAYLOAD/requirements-reference.txt" "$PAYLOAD/requirements.txt"
(cd "$PAYLOAD" && func azure functionapp publish "$APP" --python --build remote)
cd "$ROOT"
```

The `.funcignore` excludes local environments, settings and caches; the observed
source upload was 13.92 KB, not a local venv. Keep the payload free of secrets.
Identity roles are created after the Function App identity exists. Allow RBAC
propagation before diagnosing storage failures; do not fall back to storage keys.
The first cycle's `az functionapp show` returned null Flex properties: use the
ARM query below, not that null as evidence of absent runtime settings.

```bash
az resource show --resource-group "$RG" --name "$APP" \
  --resource-type Microsoft.Web/sites --api-version 2024-04-01 \
  --query '{runtime:properties.functionAppConfig.runtime,scale:properties.functionAppConfig.scaleAndConcurrency,auth:properties.functionAppConfig.deployment.storage.authentication.type}'
az storage account show -g "$RG" -n "$ST" \
  --query '{sharedKey:allowSharedKeyAccess,publicBlob:allowBlobPublicAccess,tls:minimumTlsVersion}'
az functionapp config appsettings list -g "$RG" -n "$APP" --query '[].name'
```

## Run the isolated verification harness

```bash
export REF882_BASE_URL="https://$APP.azurewebsites.net"
export REF882_RESOURCE_GROUP="$RG" REF882_APP="$APP"
export REF882_EVIDENCE="$PWD/reference-evidence.json"
REF882_FUNCTION_KEY=$(az functionapp keys list -g "$RG" -n "$APP" \
  --query functionKeys.default -o tsv)
export REF882_FUNCTION_KEY
hatch run python -m pytest tests/e2e_reference -m e2e_reference \
  -o addopts='' --tb=no --show-capture=no -q
unset REF882_FUNCTION_KEY
```

The harness checks anonymous liveness, rejected keyless and successful keyed
invoke, deployed installed-distribution versions, two-request accumulated state
through the state endpoint, cross-thread isolation, four concurrent same-thread
requests and persistence across `az functionapp restart`. The runtime endpoint
`GET /api/reference/runtime` is function-key protected and returns only versions.
Run only against an expendable app: the restart is disruptive.

Concurrent invokes may return **200 or 409**: the default lock waits up to five
seconds, so short requests can serialize successfully rather than yield exactly
one winner. At least one must succeed. Persisted state must contain every accepted
turn exactly once and its matching assistant turn, and no rejected input. A later
append must work. This is not a fairness or cross-instance scale-out assertion.

Missing base URL/key skips the live tests; missing group/app skips restart. A
verification run requires **all five tests passing, zero skips**. Evidence JSON
contains allow-listed status/count/timing tags and numeric versions only, not keys,
URLs, bodies or resource IDs. Use `--tb=no --show-capture=no`; never `--showlocals`.
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
| 409 on invoke | Same-thread lock acquisition budget exhausted; bounded client backoff, no blind replay of uncertain side effects |
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
| Auth test extra parser | Failed on missing direct message `content`; invoke uses `data.content`; corrected offline, **not rerun live** |
| Installed runtime versions | Passed, table above |
| Same-thread persistence and cross-thread isolation | Passed; 3 invokes plus state reads; 21.728 s |
| Four concurrent invokes | 409 / 200 / 409 / 200; 0.644 s; both accepted turns persisted, rejected absent; later append passed |
| Restart persistence | Passed; state before restart preserved and next turn accumulated; 7.212 s including Azure restart/readiness/state/invoke |
| Live pytest aggregate | **4 passed / 1 failed / 0 skipped**, 35.77 s; not all-green certification |

Three provision cycles, no fourth: first stopped on evidence wrapper's null CLI
properties; second exposed example's protected-liveness misconfiguration; third
produced the observations above and found the harness envelope mismatch. None
established a package defect. Final app/IaC was not altered after cycle 3.

| Created group | Contents | Deletion evidence |
| --- | --- | --- |
| `rg-af-ref-202611242144` | FC1 plan, Function App/system identity, Standard_LRS account, Blob containers, one account role | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |
| `rg-af-ref-202611242148` | Same topology, published app | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |
| `rg-af-ref-202611242157` | Same topology, corrected example | EXIT delete/wait; exists false; no remaining owned `rg-af-*` names |

Resource group names above identify disposable evidence resources, not reusable
deployment names. Every group lived for minutes, below the three-hour limit.
Local sanitized logs, evidence JSON, JUnit and command durations are under
`/tmp/opencode/ref882/`; that workstation directory is **not a durable CI artifact**.
The essential evidence is recorded here; no credentials/subscription IDs are included.

## Cost, cold starts and teardown

Estimated marginal cost for all three short-lived cycles: **under US$1 total**,
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
az group list --query "[?starts_with(name,'rg-af-ref-')].name" -o tsv
rm -rf "$PAYLOAD"
```

Confirm `exists=false` for your group and that your owned name is absent from the
list; never delete another person's group. The group deletion removes the plan,
app/system identity, account/data/containers and scoped role assignment. Check
Cost Management later for actual charges. Maintainer follow-up: authorize final
SHA/RBAC, obtain an all-green corrected-harness run under new spend approval,
and decide a separate re-certification workflow; do not close #882 automatically.
