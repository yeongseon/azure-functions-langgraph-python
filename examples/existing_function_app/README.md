# LangGraph in an existing Functions app

Keep an existing Python v2 `FunctionApp`, its HTTP trigger and its timer trigger,
then attach native graph routes with the **Experimental**
`LangGraphApp.to_blueprint()` API. This example requires a build containing that
method; the currently published package may not contain it yet. Install the
candidate wheel into your local environment before running this example.

## What changes

The HTTP function at `GET /api/existing` and the five-minute timer are defined
first. The timer body is deliberately a no-op: replace it with your existing
application job. The only LangGraph integration is:

```python
langgraph_app = LangGraphApp()
langgraph_app.register(graph=graph, name="official_basic")
app.register_functions(langgraph_app.to_blueprint())
```

Do not export `langgraph_app.function_app` alongside `app`. The worker needs one
application instance. Complete graph registration before export; do not register
the same Blueprint twice. The [composition guide](../../docs/usage.md#existing-app-composition-experimental)
describes naming, health endpoint conflicts, timing, authorization and topology.

## Unchanged upstream graph

`graph.py` contains the same three snippets as
[`official_langgraph_basic/upstream_graph.py`](../official_langgraph_basic/upstream_graph.py):
LangChain's [Define and update state](https://docs.langchain.com/oss/python/langgraph/graph-api-state)
guide, pinned to `langchain-ai/docs@42bc913ad89c7f288667184ae9fc485d9fa28023`.
Its MIT notice is included. No nodes, state fields, edges or compile call change;
only Functions hosting differs. It makes no model calls and needs no cloud keys.

For input `{"messages":[{"role":"human","content":"Hi"}]}`, the baseline
keeps that input message, appends an AI message with content `Hello!`, and sets
`extra_field` to `10`. The adapter wraps the result in `output` and serializes the
AI message using LangChain's `message_to_dict`. The smoke test compares the whole
serialized output with an independently executed pinned upstream baseline.

## Run locally

Prerequisites: Python 3.11+, Azure Functions Core Tools v4, Azurite, and a local
environment with the candidate adapter wheel and `requirements.txt` dependencies.
Build the candidate from the repository root with `make build`; use your normal
isolated environment to install that wheel rather than replacing a shared SDK.

Start Azurite in a separate terminal (default ports, scratch data directory):

```bash
azurite --silent --location .azurite
```

Configure **only your local process environment**, not checked-in credentials:

- `FUNCTIONS_WORKER_RUNTIME=python`
- `AzureWebJobsStorage=UseDevelopmentStorage=true`

From this example directory, with your Python environment active:

```bash
func start
```

From another terminal:

```bash
curl --fail http://localhost:7071/api/existing
curl --fail http://localhost:7071/api/health
curl --fail http://localhost:7071/api/graphs/official_basic/invoke \
  -H 'Content-Type: application/json' \
  -d '{"input":{"messages":[{"role":"human","content":"Hi"}]}}'
```

The first response must be `existing HTTP still works`; the graph result must
match the upstream baseline above. Stop **both** the Functions host and your
Azurite process with Ctrl-C when finished. Do not terminate other local hosts.

Expected discovery: `existing_http`, `existing_timer`, `aflg_health`,
`aflg_health_details`, `aflg_official_basic_invoke`,
`aflg_official_basic_stream`, `aflg_official_basic_state`. State is discoverable
because the compiled graph supports it; without a checkpointer it does not
provide persistent thread state. Streaming is buffered SSE.

## Validation scope

`tests/test_existing_function_app_example.py` loads this entrypoint using the
repository's isolated example loader. It runs in source and installed-wheel
test lanes without importing `src/` directly. The existing HTTP response, timer
binding and complete route inventory are asserted alongside upstream parity.

Local Core Tools/Azurite results are recorded in the implementing PR. They do
not certify a deployed Azure plan or timer recovery. Core Tools does not enforce
function keys by default; deployed HTTP routes require keys, except anonymous
liveness.

## Azure evidence (2026-11-27)

One maintainer-authorized manual deployment for #993, not an automated lane, a
release certification or a claim about other plans. It deployed a copy of this
directory with the adapter built from `main` at `bd01cc2` (wheel
sha256 `d4f8568bc27c623fb8d03c7e9b48c34399bb015d48549bafd959f129fdf25a82`, `0.14.2`, built with `uv build --wheel`) bundled under
`wheels/` and referenced from `requirements.txt` by local path. `to_blueprint()`
is not in the published `0.14.2`, so a successful registration also shows the
bundled wheel, not PyPI, was installed. One temporary resource group, deleted
afterwards.

| Item | Value |
| --- | --- |
| Plan / runtime | Flex Consumption, Python 3.11, `koreacentral`, Functions v4 |
| Tools | Azure CLI 2.83.0, Core Tools 4.8.0: `func azure functionapp publish <APP> --python --build remote` |
| App setup | `az functionapp create ... --disable-app-insights true`, so no Application Insights or default Log Analytics workspace was created |
| Trigger inventory | 7 functions: `existing_http` (HTTP), `existing_timer` (timer, `0 */5 * * * *`, `runOnStartup=false`, `useMonitor=true`), `aflg_health`, `aflg_health_details`, `aflg_official_basic_invoke`, `aflg_official_basic_stream`, `aflg_official_basic_state` |
| `GET /api/health` (no key) | `200`, `{"status":"ok"}` |
| `GET /api/existing` without key / with function key | `401` / `200`, `existing HTTP still works` |
| `POST /api/graphs/official_basic/invoke` without key / wrong key | `401` / `401` |
| Same call with the function key | `200`; `messages` = `Hi`, `Hello!`, `extra_field` = `10` (equal to the upstream baseline) |
| `POST /api/graphs/official_basic/stream` with key | `200`, 3 SSE frames (two `data`, one `end`) |
| Cleanup | Resource group deleted and the deletion awaited; `az group exists` returned `false`; no tagged resource remained and no `DefaultResourceGroup-*` was created |

Not exercised: a timer firing, timer recovery after a host restart, thread
storage, other plans or regions, and multi-user authorization. First requests
can include a cold start, so no latency is recorded.

### Entra and telemetry follow-up attempt (2026-11-28 UTC)

A maintainer-authorized experiment for [#995](https://github.com/yeongseon/azure-functions-langgraph-python/issues/995)
attempted the combined existing-app composition, Flex/Blob persistence,
trusted Entra ingress and Application Insights path. **It stopped before
publishing an app. No authorization, telemetry ingestion or worker-turnover
claim follows from this attempt.** The earlier successful composition run above
and the [separate Flex worker-turnover evidence](../../docs/production-reference.md)
remain distinct; neither proves the combined path.

| Item | Observed result |
| --- | --- |
| Candidate source | `origin/main` at `6754c6c`; built with `uv build --wheel`, adapter `0.14.2` |
| Candidate wheel SHA256 | `fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3` |
| Tools | Azure CLI `2.83.0`, Core Tools `4.8.0`, uv `0.11.7` |
| Provisioned topology | Existing `infra/flex.bicep`, identity mode off: FC1 Flex Consumption, Python `3.12` configured, `512` MB, Korea Central, Standard_LRS storage |
| Host/deployment storage | Connection-string settings from the existing template; not identity-only host storage |
| Entra setup | One temporary, explicitly named single-tenant app registration created; API-exposure configuration via Microsoft Graph `PATCH` returned Azure CLI exit `1` |
| Failure detail | No diagnostic/error code was retained, so permission, API validation and transient failure cannot be distinguished; no privilege elevation or alternate configuration attempted |
| Publish/runtime | Not reached; no deployed wheel or running Python/package inventory established |
| Two-principal authorization | Not run: owner resume, authenticated non-owner invoke/state denial, and unchanged checkpoints remain unverified |
| Telemetry/turnover | Not run: no workspace/component created, no lifecycle/request correlation query, no restart or changed-worker-token check |
| Cleanup | Owned resource group deleted synchronously; temporary Entra app deleted; checks below completed before this section was written |

The experiment wrapper was interrupted by its terminal's 120-second timeout
while cleanup was in progress. Cleanup was then completed explicitly for those
same owned objects, without touching unrelated groups. This is a collection
failure, not an established library defect. Raw credentials and identifiers
are not retained in this document. Sanitized verification output:

```text
az group exists -n <owned-group>: false
az resource list --tag purpose=995-verify --query 'length(@)': 0
az group list --query "[?starts_with(name, 'DefaultResourceGroup-')].name": []
az ad app list --all --query 'length(@)': 2 (baseline: 2)
az ad sp list --display-name <owned-registration-name> --query 'length(@)': 0
```

Remaining recipe requirements are unchanged: require trusted ingress on every
route and do not authenticate by decoding caller headers; enforce server-owned
thread ownership for both RUN and STATE_READ; keep host/application storage
identity separate from end-user identity; and verify sanitized lifecycle events
against request telemetry on the owned Log Analytics workspace. A restart must
show a different protected worker token plus preserved state, not merely an
accepted restart command. No unverified recipe code, new CI gate or workflow
change accompanies this blocked-run record.

### Bounded retry: Entra root cause and live authorization (2026-11-28 UTC)

The maintainer authorized a sequential retry, first reproducing the Entra step
without any Azure resources. Full stdout/stderr was captured locally with IDs
redacted. Microsoft Graph returned `InvalidValue`:

```text
Property api.preAuthorizedApplications.delegatedPermissionIds has a Permission Id
that cannot be found in the AppPermissions sets.
```

The original PATCH simultaneously created a scope and referenced it from
`preAuthorizedApplications`. Two sequential PATCHes (scope first,
preauthorization second) both returned exit `0`. No directory-permission
elevation was required. Both isolation registrations were deleted, returning
the app-registration count to `2` after each probe. The custom scope is for the
Azure CLI delegated test token, **not a prerequisite for Easy Auth bearer
validation**. It preauthorizes the Azure CLI client only on the temporary API;
it changes no other Entra application or user's permissions.

The live attempt used the same candidate wheel SHA256 recorded above and
`infra/flex.bicep` identity mode off: FC1, `512` MB, Korea Central, Python `3.12`,
no always-ready instances. The staged entrypoint was
[`reference_function_app.py`](reference_function_app.py), copied to
`function_app.py` in a scratch copy of this existing example. It retains the
HTTP/timer + one Blueprint model and recompiles the existing graph with the
Blob saver and lease lock. The wheel was bundled under `wheels/` and installed
from `./wheels/<candidate>.whl[azure-blob,azure-identity]`, with `langchain` and
`pydantic`; no PyPI adapter substituted for it. Publish used Core Tools `4.8.0`
`func azure functionapp publish <app> --python --build remote`, after a
30-second creation wait. Azure CLI was `2.83.0`.

| Check | Live result |
| --- | --- |
| Remote build/publish | Exit `0`; protected inventory reports adapter `0.14.2`, Python `3.12.16`, azure-functions `1.26.0`, langgraph `1.2.24`, azure-storage-blob `12.31.1`, azure-identity `1.27.0` |
| Distinct principals | Temporary app's service principal as owner; logged-in Azure CLI principal as non-owner; object IDs differed (not retained here) |
| Owner invoke / state read | `200` / `200`; checkpoint values read successfully |
| Non-owner authenticated runtime probe | `200`, establishing that the second bearer token passed ingress and application tenant parsing |
| Non-owner invoke / state read | `403` / `403` on the same existing thread |
| Owner state after denial | `200`; complete parsed state response equal to the pre-denial response |
| Forged principal header without bearer token | `401`, even with a valid function key |
| Restart | Azure command accepted; 20 protected probes at 10-second intervals did not observe a changed process token; **turnover not confirmed** |
| Post-turnover state/resume | Not reached; no claim of restart persistence or owner resume after worker replacement |
| Telemetry | Own workspace and workspace-based Insights component provisioned and connection configured; harness stopped at the turnover assertion before Log Analytics queries; **ingestion/correlation unverified** |

#### Verified wiring and boundaries

[`reference_policy.py`](reference_policy.py) implements application-owned policy,
not JWT validation. The deployment used `authsettingsV2`: platform enabled,
`requireAuthentication=true`, `unauthenticatedClientAction=Return401`, HTTPS
required, single-tenant Entra issuer `https://login.microsoftonline.com/<tenant>/v2.0`,
and accepted audiences `<client-id>` and `api://<client-id>`. No excluded route
or alternative ingress was configured. Trust `x-ms-client-principal` only behind
that platform boundary, which strips/replaces caller identity headers; do not
deploy this parser behind an untrusted direct path. It uses mapped tenant/object
claims, never names, body owner fields or an unverified bearer token.

The operator preprovisions `owners/<sha256(graph-name:thread-id)>.json` in the
private checkpoint container with `{"tenant":"<tenant>","principal":"<owner-object-id>"}`.
Both RUN and STATE_READ compare that record on each admission. Missing records,
wrong graph/tenant and threadless runs fail closed. Requests cannot create or
transfer ownership. No ownership-on-first-request or concurrent ownership
creation claim is made. This static entitlement recipe does not cover Platform
collections, Durable management, revocation of already admitted work or tenant
retention/deletion policy.

| Setting / grant | Purpose and actual scope |
| --- | --- |
| `AzureWebJobsStorage`, `DEPLOYMENT_STORAGE_CONNECTION_STRING` | Connection-string **host/deployment** storage; Shared Key remains enabled for this run |
| `AZURE_STORAGE_BLOB_ACCOUNT_URL`, `LANGGRAPH_BLOB_CONTAINER` | Application checkpoint, lease and owner-record container through system-assigned Managed Identity |
| Storage Blob Data Contributor | Exactly one role assignment, app system identity, scoped only to the owned storage account; explicitly deleted before group deletion |
| `REFERENCE_TENANT` | Expected ingress tenant; identifier configuration, not user authentication by itself |
| `LANGGRAPH_STRICT_MSGPACK=true`, `AZFUNC_LANGGRAPH_LOCK_BACKEND=distributed` | Serializer restriction and fail-fast distributed lock requirement; leases are not checkpoint fencing |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Own workspace-based component; no default workspace created |
| `AzureFunctionsJobHost__logging__logLevel__default=Information`, `AzureFunctionsJobHost__logging__applicationInsights__samplingSettings__isEnabled=false` | Requested host lifecycle log forwarding without sampling; not proof of ingestion |

`LoggingRunObserver` emits started/completed/rejected events using the existing
safe graph, endpoint, request/run correlation, status and rejection-reason
fields. Caller-selected request IDs are correlation, not authentication or
unique execution proof. No principal IDs, tokens, function keys, ownership
records or checkpoint payloads were added to application logs. Native host logs
and storage diagnostics have their own classification/access rules. A future
authorized run must query **Log Analytics** with the workspace customer ID,
allowing several minutes for ingestion, for example:

```kusto
AppTraces
| where Message has "<known-request-id>"
| project TimeGenerated, Message, OperationId, Properties
```

Join its `OperationId` to `AppRequests.OperationId` and inspect the lifecycle
events and request result. That query was **not executed in this attempt**;
structured-field ingestion and end-to-end correlation remain unproven.

#### Retry cleanup

The harness deleted the scoped role assignment, waited for resource-group
deletion, then deleted the temporary service principal and app registration in
`finally`. All checks below completed before writing this retry section:

```text
owned group exists: false
resources tagged purpose=995-verify: 0
DefaultResourceGroup-* groups: []
Entra app registrations: 2 (baseline: 2)
owned app registrations: 0
owned service principals: 0
created storage role assignment: deleted (command exit 0; scope resource deleted)
```

This retry establishes live admission isolation and denial non-mutation for one
app/region/runtime, not all acceptance items, restart continuity, telemetry
certification, scale-out, exactly-once execution or a latency/cost claim. No
additional deployment was attempted after cleanup. Offline policy tests cover
the verified admission decisions; Azure-specific checks remain manual and
opt-in, with no new required CI or release gate.

### Independent telemetry and turnover follow-up (2026-11-28 UTC)

One new maintainer-authorized deployment followed [PR #1076](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1076),
using current `origin/main` at `07a2967`. Telemetry collection ran **first** and
was saved independently of the later turnover stage. All Azure resources were
deleted and cleanup verified **before this section was written**. This is manual
evidence for [#995](https://github.com/yeongseon/azure-functions-langgraph-python/issues/995),
not a new CI or release certification gate.

| Deployment input | Recorded value |
| --- | --- |
| Adapter wheel | `0.14.2`, built with `uv build --wheel` from `07a2967`, bundled under `wheels/` and installed by local-path requirements |
| Wheel SHA256 | `fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3` |
| Staged recipe | This PR's `reference_function_app.py`, with the protected correlated checkpoint probe; SHA256 `8ca39d29e2d813769fab3f16655ae9f300d1d996ef901e1d129d59069b6e0218` at publish time |
| Tools | Azure CLI `2.83.0`, Core Tools `4.8.0`, uv `0.11.7`; remote publish exit `0` |
| Topology | Existing `infra/flex.bicep`, FC1 Flex Consumption, Python `3.12`, `512` MB, Korea Central, Standard_LRS storage, no always-ready instances |
| Storage boundary | Connection-string host/deployment storage; system-assigned Managed Identity for application checkpoints, leases and owner records; one owned-account-scoped Storage Blob Data Contributor assignment |
| Telemetry resources | Own workspace-based Application Insights component and Log Analytics workspace inside the disposable group; no default workspace/group |
| Protected runtime | Python `3.12.16`, adapter `0.14.2`, azure-functions `1.26.0`, langgraph `1.2.24`, azure-storage-blob `12.31.1`, azure-identity `1.27.0` |

The wheel itself is unchanged by the recipe edit. The entrypoint was staged from
the working tree, not claimed to be the unmodified `origin/main` recipe. Its
published hash above precedes local typing refinements (an explicit lock variable
and typed, normalized probe input) and a docstring clarification. Those final
refinements have offline coverage, not a second cloud run. No paid
model, separate database, subscription-wide grant or provider registration was
used. Easy Auth configuration and the settings above were retained; the owner
was one temporary credentialed service principal. No custom delegated scope or
CLI preauthorization was needed for this owner-only follow-up. The distinct
non-owner and forged-header checks from #1076 were **not repeated** here.

#### Captured telemetry, before turnover

The owner sent one native invoke with `X-Request-ID: aflg995-followup-known`;
it returned `200`. Host information logging and disabled sampling were configured
as above. The following query ran against **the owned Log Analytics workspace**
with `az monitor log-analytics query --workspace <customer-id> --analytics-query
<query>`, retrying at one-minute intervals. The third query returned the rows
below. This reports the query interval/count, not measured ingestion latency.

```kusto
let ops = AppTraces
| where TimeGenerated > ago(1h)
| where Message has 'aflg995-followup-known'
| distinct OperationId;
union
(AppTraces
 | where OperationId in (ops)
 | extend Event = extract(@'(langgraph\.run\.\w+)', 1, Message)
 | where isnotempty(Event)
 | project Kind='trace', Event, OperationId, Status='', Success=''),
(AppRequests
 | where OperationId in (ops)
 | project Kind='request', Event='', OperationId,
           Status=ResultCode, Success=tostring(Success)),
(AppExceptions
 | where OperationId in (ops)
 | project Kind='exception', Event='', OperationId, Status='', Success='')
```

| Kind | Event | HTTP status / success |
| --- | --- | --- |
| request | — | `200` / `True` |
| trace | `langgraph.run.started` | — |
| trace | `langgraph.run.completed` | — |

All three rows shared **one operation correlation**. The query also
included `AppExceptions`; no correlated exception row was returned. Operation
IDs were compared in memory and omitted from saved notes; only kind, lifecycle
event, status, success and counts were retained. No raw `Message`, `Properties`,
principal, thread identifier, credentials or checkpoint content was exported.
The known request ID appeared in lifecycle messages as the safe `run_id` summary;
the host attached the operation correlation. This establishes request-to-graph
lifecycle correlation for one successful invoke, **not** ingestion of the
observer's complete structured `langgraph_run` extras, denial telemetry, failure
telemetry, alerting, uniqueness of caller IDs or exactly-once execution.

#### Correlated worker turnover and owner resume

The method follows [PR #1049](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1049):
the private per-import worker token and the checkpoint read must come from the
**same protected response**, with `Cache-Control: no-store`. Separate runtime
and native state responses do not prove which worker read the state.

This opt-in entrypoint now accepts `restart_thread=restart-<32 hex digits>` on
the existing runtime route. The operator provisions its owner record first.
POST checks RUN authorization, acquires the existing distributed lease lock,
writes the deterministic `before` turn, releases the lock in `finally`, and
reads the checkpoint. GET checks STATE_READ authorization and reads that same
checkpoint. Both responses include the private process token and normalized
message values, never identity claims. This graph is deterministic and makes no
model calls; its upstream graph definition is unchanged. The probe does not
emit native invoke lifecycle events and is not an alternative public graph API.

| Check | Actual observation |
| --- | --- |
| Pre-turnover correlated POST | `200`; values contained `before`, `Hello!` |
| `az functionapp restart` | Accepted; 120 GET probes over the five-minute polling budget did not confirm turnover |
| `az functionapp stop` then `start` | Both accepted; changed token observed on probe 6 within the second five-minute budget; one transport error recorded during polling |
| Changed-worker checkpoint response | `200`; token differed from the pre-turnover writer; complete normalized message values equal to the pre-turnover response |
| Owner native resume | `200`; input supplied the prior normalized messages plus `after`, using the same entitled thread |
| Owner native state read after resume | `200` |
| Final appended-content assertion | **Not completed**: the scratch harness assumed every native state message had `data.content` and raised `KeyError: data` |

Offline preparation exposed a related probe serialization mismatch: this official
graph can retain dictionary input messages alongside `AIMessage` objects.
`convert_to_messages` before `messages_to_dict` normalized the probe response;
the focused test then passed and that recipe was published. The final native
state assertion did not apply that normalization. Its `KeyError` is a harness
finding, not an established persistence defect. Raw native responses were not
saved, so the final accumulated message contents cannot be reconstructed or
claimed verified after deletion. No extra deployment was made to repair the
evidence gap.

Confirmed different-serving-process state continuity and successful authorized
resume are observed; complete post-resume accumulated contents remain unverified.
The restart command alone did **not** establish turnover in this run; stop/start
did. No assertion is made about retirement of every old worker, restart during
active work, cancellation, scale-out, long-running workflows, other runtimes or
regions, identity-only host storage, fencing, cold starts, latency or price.

#### Immediate verified cleanup

The final assertion raised, but `finally` still deleted the account-scoped role,
awaited group deletion, and deleted the temporary credentialed service principal
and app registration. It then captured this sanitized output before any docs:

```text
az group exists -n <owned-group>: false
az resource list --tag purpose=995-verify --query 'length(@)': 0
az group list --query "[?starts_with(name, 'DefaultResourceGroup-')].name": []
az ad app list --all --query 'length(@)': 2 (baseline: 2)
owned app registrations: 0
owned service principals: 0
created storage role assignment: deleted successfully
cleanup failures: []
```

Unrelated resource groups were not modified or used. The system-assigned app
identity was removed with its app; the short-lived credential was removed with
its registration. Billing was not measured. This record uses `Refs #995` only;
the maintainer decides acceptance, with issue checkboxes unchanged.
