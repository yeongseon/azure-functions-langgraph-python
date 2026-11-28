# Production backend decision: Flex, Blob/Table and Postgres

## Decision and learning paths

Retain the maintainer's [2026-11-24 decision on #882](https://github.com/yeongseon/azure-functions-langgraph-python/issues/882):
**Flex Consumption with Blob checkpoints and a Blob lease lock is the native
production-oriented reference.** Add Azure Table thread metadata only when using
the optional Platform surface. Postgres remains an advanced candidate, not the
universal default or a certified replacement. The bounded 2026-11-28 attempt below
could not provision the authorized database SKU; it does not overturn that decision.

| Path | Starting point | Boundary |
| --- | --- | --- |
| Beginner / local demo | [5-minute tutorial](tutorial-5-min.md), [simple agent](examples/simple_agent.md) | Minimal stateless first run; SQLite demonstrations are process/local-disk oriented, not shared production persistence |
| Production-oriented native golden path | [Flex Blob reference](production-reference.md) | One canonical `infra/flex.bicep`; completed checkpoints plus lease coordination, not fencing or exactly-once execution |
| Lower-baseline-cost Azure alternative to a database | Blob checkpoints + Blob lease lock, optional Table thread store | Reuses Azure Storage instead of adding continuously provisioned database compute; relative cost is architectural, not a measured tariff or workload benchmark |
| Advanced / existing Postgres estate | [Postgres example](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/postgres_checkpoint_production), [distributed deployment guide](production-guide.md#distributed-deployment) | Checkpoints only; explicitly wire a compatible distributed lock and, for Platform routes, shared thread metadata before scale-out |

The lower-cost alternative and the selected native golden path deliberately use
the same Blob topology. There is no second production template. Keep the minimal
tutorial separate. The existing MkDocs Examples navigation already groups
**Beginner / Production / Advanced**; category placement is not certification.
No example folders or contributor-reserved work are changed here.

Keep `infra/flex.bicep` as the topology source; do not add a parallel CLI/azd
production recipe. The completed [#781 azd spike](https://github.com/yeongseon/azure-functions-langgraph-python/issues/781)
deferred azd after its duplication and setup friction; the completed
[#779 onboarding work](https://github.com/yeongseon/azure-functions-langgraph-python/issues/779)
keeps the beginner route small. This decision changes no infrastructure default.

## Existing evidence reused, not rerun

| Evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| [#976](https://github.com/yeongseon/azure-functions-langgraph-python/pull/976), 2026-11-24 | Fresh-checkout Flex FC1/Python 3.11/2048 MB native Blob reference using published wheel 0.14.2; five live tests passed, no skips; auth, persistence/isolation, concurrent requests, restart availability and verified teardown | Candidate-source wheel, multi-instance execution, process turnover in that historical restart, Table or Postgres |
| [#1021](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1021) | Harness now rejects keyless runtime access; offline negative cases | New live proof by itself |
| [#1028](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1028) | Harness correlates checkpoint reads with a changed process token; offline old-worker/timeout cases | New live proof by itself |
| [#1049](https://github.com/yeongseon/azure-functions-langgraph-python/pull/1049), 2026-11-26 | Second Flex/Blob run, five live tests passed, no skips, published wheel 0.14.2; keyless runtime 401/keyed 200; changed serving process read the pre-restart checkpoint; ownership-checked cleanup after failure and success | Retirement of every worker, interruption of active work, load/soak or natural scale-to-zero |

The [dated reference observations](production-reference.md#dated-azure-evidence-2026-11-26-utc)
remain the authority for their actual runtime inventories and timings. They are
two successful deployments, not exact dependency replay or release certification.
The supported package windows remain Python `>=3.11,<3.15`, LangGraph `>=1,<2`,
azure-functions `>=1.21,<2` below Python 3.13 (and `<3` on 3.13+). The Postgres
extra remains `langgraph-checkpoint-postgres>=3,<4` and `psycopg[binary]>=3,<4`.
Bounds are not resolved pins or proof of every version in the window.

Recent [MI storage evidence](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/managed_identity_storage)
also records accepted Flex publish with zero indexed functions. That failure does
not establish its root cause. Historical MI success is not a promise that every
identity-only host configuration works. The planned comparison below therefore
kept **connection-string host/deployment storage**, while leaving the canonical
template default and historical identity reference unchanged. Application-data
MI and host-storage authentication are separate choices.

## Comparison: facts, constraints and remaining tests

This table combines repository behavior and linked service documentation; it is
**not a live Postgres performance comparison**. The provisioning failure prevented
all application/database measurements.

| Concern | Selected Blob/Table path | Flex + Postgres checkpoints candidate |
| --- | --- | --- |
| Scale-to-zero | Flex can scale to zero; Storage has capacity/transaction charges, not a database compute instance | Flex can scale to zero but PostgreSQL Flexible Server does not automatically follow it; a stopped server automatically starts after seven days ([limits](https://learn.microsoft.com/en-us/azure/postgresql/configure-maintain/concepts-limits)). Natural idle/cold-start/reconnect behavior untested |
| Connections | HTTPS storage requests; account throttling still applies | `create_postgres_checkpointer` opens one long-lived connection per saver/worker, not a pool. Documented B1ms default: 50 total / 35 user connections; reserved slots must be checked on the actual server. The template's max 40 instances could exceed 35 even at one saver/worker; overlapping old/new workers and other clients need headroom |
| Pooling | No SQL pooler | Built-in PgBouncer is **not supported on Burstable** ([PgBouncer](https://learn.microsoft.com/en-us/azure/postgresql/connectivity/concepts-pgbouncer)). A client pool does not remove the database connection budget. The helper defaults to eager preparation (`prepare_threshold=0`); use `None` for transaction pooling without prepared-statement support and test the chosen pooler. No pool, limit-exhaustion or reconnection measurements were obtained |
| Same-thread concurrency | Existing live reference accepts 200/409; every accepted input persisted, rejected inputs absent. Concurrent HTTP requests need not imply overlapping execution | Database persistence is not a thread lock. No Postgres lock or Postgres thread store is shipped. A Blob lease lock can be supplied through `ThreadLock`; all writers must share graph/lock namespace and the same backend. Hybrid contention remains untested |
| Lost-owner writes | Blob lease does not fence checkpoint writes; the optional guard acts only after detected loss and is off in the reference | Blob checkpoint guard does not fence Postgres writes. A hybrid cannot inherit Blob-specific checkpoint lock-scope identity or ownership guards. No exactly-once or side-effect rollback claim |
| Platform metadata | Optional Azure Table; native reference does not provision it | Postgres checkpoints do not make in-memory Platform thread metadata durable. A shared Table store is still an explicit separate choice |
| Networking | Historical public HTTPS ingress/storage, TLS minimum and anonymous Blob disabled; private networking untested | Requested public DB access with only the short-run Azure-services rule (`0.0.0.0` to `0.0.0.0`), not all internet IPs. This allows other Azure customers' resources and is not app-only isolation. Production requires deliberate outbound-IP allowlisting or separately verified private networking. No usable server/firewall or TLS handshake was observed |
| Secrets / identity | Historical app MI with account-scoped Blob Data Owner; no new roles here | Helper receives a connection string; no token-refresh callback or reconnect lifecycle. A startup Entra token alone is not token-refresh verification. Planned temporary password was generated in memory and never logged; no Key Vault, MI database login, rotation or least-privilege database role was tested |
| Cost | Existing reference records short-lived resource durations and explicitly estimated, not invoiced, cost | Database adds provisioned compute/storage and operational work. Failed provision and teardown timings below are measured; currency charges, cold-start latency, steady-state price and a performance ratio are not |

Before promoting Postgres, an authorized run must prove database reachability,
least-privilege migration/runtime roles, connection-slot headroom across worker
turnover, pooled prepared-statement compatibility if used, credential renewal,
same-thread shared-lock behavior and persistence/reconnect after idle/restart.
Do not raise the instance count, database SKU or connection limit to hide a failure.
No new lock implementation, database IaC or required CI/release gate is introduced.

## Bounded Azure attempt — 2026-11-28 UTC

Source: clean worktree of `origin/main` at
`8e6fefa8bcc1fda02cfc1cfdba4b3bc124356340`. Tools: Azure CLI **2.83.0**,
Bicep **0.41.2**, Core Tools **4.8.0**, uv **0.11.7**. Subscription state was
Enabled. The PostgreSQL provider was already Registered; no registration,
subscription-level role or tenant change was attempted.

`uv build --wheel` succeeded before provisioning. Candidate:
`azure_functions_langgraph-0.14.2-py3-none-any.whl`, SHA-256:

```text
fca98285bc97dd2929674e329f3343ba8eb3404a4512eceb2ab433102dd98be3
```

This is a **local candidate build, not a deployed artifact**. Planned continuation
was the existing Flex template, Linux FC1, Python **3.12**, **512 MB**, no
always-ready capacity, Standard_LRS host storage with connection strings, and a
bundled candidate wheel using the existing example payload pattern. The database
was the first provisioning gate to avoid paying for an app when unavailable.
No Function App, plan or Storage Account was created, no wheel was uploaded and no
runtime/dependency inventory was collected from Azure.

| Operation | Actual result |
| --- | --- |
| Owned group | `rg-aflg-882-verify-184c71e9`, Korea Central, `purpose=882-verify owner=opencode-temp`; absent before create |
| Database request | PostgreSQL Flexible Server 16, Burstable **Standard_B1ms**, **32 GiB** minimum CLI storage, autogrow disabled, seven-day backup retention, geo backup/HA disabled, temporary password auth, Entra auth disabled, public Azure-services-only rule requested |
| Request timing | 02:25:14–02:27:20 UTC, **126 s**, Azure CLI exit **1**; 900 s timeout not reached |
| Azure error | **`SkuNotAvailable`**: “Specified SKU is not supported in this region. Please choose a different SKU.” |
| Immediate owned-group inventory | One partial server: `sku=Standard_B1ms`, `storageGiB=32`, **`state=Dropping`**; not a usable database |
| Scope decision | No higher tier, alternative region, provider registration, permission elevation or second provision attempt. The error proves this request was rejected, not a permanent region-wide ban or a quota diagnosis |
| Cleanup timing | Armed EXIT cleanup verified owner/purpose, deleted the group and awaited deletion, 02:27:21–02:31:28 UTC, **247 s**; resource lifetime about six minutes |

Sanitized cleanup output, collected **before writing this page**:

```text
az group exists -n <owned-group>: false
az resource list --tag purpose=882-verify: []
resource groups after cleanup: the same five unrelated groups as before the run
DefaultResourceGroup-* remaining: none (also none at baseline)
Entra app registrations: 2 before / 2 after
```

No secrets, role assignments, app registrations or telemetry resources were
created outside the owned group. No unrelated group was modified or used for
deployment. The generated temporary administrator password existed only in the
process environment/CLI argument; shell tracing was off and output was sanitized.
The Azure-services firewall was only a requested setting on the failed server,
not a verified isolation test. Actual billing was not queried.

## Acceptance disposition

- **Coherent reference to endpoint/removal:** reuse the two published-wheel Blob
  runs and existing instructions, with their recorded workstation deviations.
  This attempt adds no new clean-room deployment success; candidate-wheel
  Postgres publish/auth/persistence/contention/restart remain blocked.
- **Named tested combinations and gaps:** historical FC1/Python 3.11/Blob results
  remain dated evidence. FC1/Python 3.12/512 MB/Postgres was planned, **not deployed**.
  Postgres scale-to-zero, pools, connection limits, networking, MI and cost behavior
  remain unvalidated. No universal certification or fully pinned replay is claimed.
- **Contributor reservation:** no #817–#826 work, example restructuring or
  contributor-owned PR content was implemented. Existing navigation is reused.

Use **Refs #882**, not an automatic issue disposition. The maintainer decides
acceptance and any future authorized database run. This docs-only decision is
localized to avoid the identity/telemetry follow-ups on the shared reference page;
integrate those findings without treating them as Postgres evidence.
