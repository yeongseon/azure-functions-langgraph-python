# Roadmap

> **Canonical tracker:** [#410 — adoption-first rolling roadmap](https://github.com/yeongseon/azure-functions-langgraph-python/issues/410).
> This page mirrors that issue for readers browsing the docs. When the two
> disagree, the issue is authoritative. Last synced: **2026-09-12**.

`azure-functions-langgraph` deploys already-built LangGraph graphs as Azure
Functions HTTP endpoints. This roadmap is deliberately **adoption-first** and
treats v1.0 as a **rolling** target: the surrounding ecosystem (LangGraph,
`azure-functions`, Python) keeps moving, so we certify against a rolling
minimum/latest window instead of freezing a single pinned stack.

> First make the existing capability easy to use through real examples and keep
> the dependency line current. Only then add the remaining runtime features.
> Reach an **expansion checkpoint** after Service Bus + Durable and re-evaluate
> toward v1.0 stabilization.

## Current status (2026-09-12)

The adoption-first sequence is effectively complete. Every example phase,
runtime-fidelity phase, observability phase, true streaming, and the Azure-native
event-driven entrypoint have shipped. **Only two roadmap items remain open:**

| Item | Phase | Status | Why it is still open |
| --- | --- | --- | --- |
| [\#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350) — `azure-functions` 2.x certification / cap removal | Phase 0 (P0) | **In progress** | CI authoring is merged (Py 3.13/3.14 compat matrix + drafted Flex Consumption e2e). The remaining step is a **real-Azure** certification run before the `<2.0.0` cap is raised. |
| [\#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408) — Durable Functions-backed async run lifecycle | Phase 6 (P2) | **Deferred** | Intentionally deferred; also depends on the modern dependency line from #350. |

Once #350 certifies and #408 lands (or is explicitly dropped), the project is at
its **expansion checkpoint** — no new feature family is added automatically.

## Guiding principles

1. **Examples before features.** A new runtime feature is not higher priority
   than making the current package usable by a first-time Azure/LangGraph
   developer.
2. **Every new feature ships with a deployable example.** No feature is complete
   if users must infer the wiring from unit tests.
3. **Keep the product boundary crisp.** This is the Azure Functions deployment
   adapter for already-built LangGraph graphs, not a reimplementation of
   LangGraph Platform.
4. **Do not duplicate sibling toolkit packages.** Compose with
   `azure-functions-logging`, `azure-functions-durable-graph`,
   `azure-functions-openapi`, etc. where their responsibilities already exist.
5. **Real Azure certification matters.** Runtime claims that depend on Functions
   behavior must be proven on a wheel-installed deployment, not source-tree
   mocks alone.
6. **Rolling window, not frozen pins.** Track a rolling minimum-supported
   version and certify against the current latest for LangGraph / langgraph-sdk
   / azure-functions / Python (see [\#421](https://github.com/yeongseon/azure-functions-langgraph-python/issues/421)).
7. **Expansion checkpoint after Service Bus + Durable.** Event Grid, Storage
   Queue, Timer, MCP, A2A, Store API, webhook/cron, and other attractive
   surfaces are gated behind an explicit re-evaluation, not automatically in
   scope.

## Per-surface stability tiers

v1.0 does not require every surface to be stable — it requires each surface to
**declare** its tier honestly.

| Tier | Meaning | Surfaces |
| --- | --- | --- |
| **Stable** | API frozen; breaking change needs a major bump | native invoke/stream/state HTTP endpoints, `LangGraphApp` construction, auth levels |
| **Beta** | Shape settled, may refine before v1.0 | checkpoint backends, thread locking, async runtime ([\#422](https://github.com/yeongseon/azure-functions-langgraph-python/issues/422)), `version="v2"` pass-through ([\#423](https://github.com/yeongseon/azure-functions-langgraph-python/issues/423)), run observer contract ([\#425](https://github.com/yeongseon/azure-functions-langgraph-python/issues/425)) |
| **Experimental** | May change or be removed | LangGraph Platform compatibility, Durable async runs ([\#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408)), Service Bus trigger ([\#409](https://github.com/yeongseon/azure-functions-langgraph-python/issues/409)) |

---

## Development sequence

### Phase 0 — unblock the modern dependency line — **P0**

New examples and runtime work must not be built on top of intentionally stale
dependency ceilings or an unproven release pipeline.

- [x] [\#333](https://github.com/yeongseon/azure-functions-langgraph-python/issues/333) — release / OIDC certification pipeline *(human-blocked)*
- [ ] [\#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350) — `azure-functions` 2.x certification / cap removal (split hosting matrix; Py 3.12 Consumption ceiling)
- [x] [\#421](https://github.com/yeongseon/azure-functions-langgraph-python/issues/421) — certify LangGraph 1.2.x / langgraph-sdk 0.4.x and adopt a rolling minimum-version window

**Exit gate:**

- Release pipeline certified.
- LangGraph 1.2.x + langgraph-sdk 0.4.x resolved, tested, and certified; rolling minimum window documented.
- `azure-functions` 2.x CI + wheel + real-Azure certification green on a plan supporting the target Python.
- Dependency caps raised only after proof.

---

### Phase 1 — adoption-first examples — **P1**

These form one obvious learning path in the main README and `examples/README.md`.

1. Real Azure OpenAI agent — [x] [\#402](https://github.com/yeongseon/azure-functions-langgraph-python/issues/402)
2. Stateful conversation / thread memory — [x] [\#403](https://github.com/yeongseon/azure-functions-langgraph-python/issues/403)
3. Tool-calling agent — [x] [\#404](https://github.com/yeongseon/azure-functions-langgraph-python/issues/404)
4. Production persistent Azure OpenAI agent — [x] [\#405](https://github.com/yeongseon/azure-functions-langgraph-python/issues/405)

**Documentation gate:**

- [x] Main README has a visible **Recommended learning path**: #402 → #403 → #404 → #405.
- [x] Every example is a standalone Azure Functions app with `function_app.py`, `host.json`, `requirements.txt`, `local.settings.json.example`, README, and smoke coverage.
- [x] Examples import/test against a built wheel where practical.
- [x] No example requires cloud credentials merely to pass CI.
- [x] At least #402 and #405 have a documented real-Azure verification path.

---

### Phase 2 — runtime correctness & production fidelity — **P1**

Close the gaps between what the runtime claims and what production users need,
each shipping with the example that proves it.

5. Native async ainvoke/astream — [x] [\#422](https://github.com/yeongseon/azure-functions-langgraph-python/issues/422) *(Beta tier)*
6. `version="v2"` pass-through — [x] [\#423](https://github.com/yeongseon/azure-functions-langgraph-python/issues/423) *(Beta tier)*
7. Production example thread-lock wiring + DESIGN.md refresh — [x] [\#424](https://github.com/yeongseon/azure-functions-langgraph-python/issues/424)

---

### Phase 3 — observability foundation — **P2**

8. Run-lifecycle observer contract — [x] [\#425](https://github.com/yeongseon/azure-functions-langgraph-python/issues/425) (#407a)
9. Application Insights / OpenTelemetry integration — [x] [\#407](https://github.com/yeongseon/azure-functions-langgraph-python/issues/407) (#407b)

**Exit gate:** operators can correlate one Azure Functions invocation to one
LangGraph run/thread and diagnose duration/failure without enabling sensitive
payload logging.

---

### Phase 4 — true HTTP streaming — **P2**

10. Opt-in true streaming transport — [x] [\#406](https://github.com/yeongseon/azure-functions-langgraph-python/issues/406)

Classic `LangGraphApp` stays backward compatible; opt-in ASGI/streaming
transport via `StreamingLangGraphApp`; first chunk proven to arrive before
completion; deployable streaming example (`examples/true_streaming_agent`).

**Exit gate:** "true streaming" means measured incremental delivery, not merely
`Content-Type: text/event-stream`.

---

### Phase 5 — Azure-native event-driven entrypoint — **P2**

11. Azure Service Bus → LangGraph trigger adapter — [x] [\#409](https://github.com/yeongseon/azure-functions-langgraph-python/issues/409)

Reordered **before** Durable: an event-driven ingress is a more common
Azure-native deployment mode than long-running async orchestration, and it
exercises the correlation/thread-lock/telemetry surfaces the async work reuses.
Queue trigger; explicit message → graph input and correlation/session →
`thread_id` mapping; retry/dead-letter preservation; standalone example.
*(Experimental tier.)*

---

### Phase 6 — asynchronous run lifecycle — **P2**

12. Optional Durable Functions-backed async runs — [ ] [\#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408) *(deferred)*

**Not** a Durable reimplementation of LangGraph topology and not a replacement
for `azure-functions-durable-graph`. Create async run, poll status/result,
cancel with documented semantics, survive disconnect of the initiating HTTP
request. Deterministic orchestrator; graph/LLM/tool code in activity context;
reuse checkpointer + thread lock; real Azure timeout/cancellation documented
from evidence; no "unlimited execution" claim. *(Experimental tier.)*

---

## Expansion checkpoint after #408 / #409

Once Service Bus ([\#409](https://github.com/yeongseon/azure-functions-langgraph-python/issues/409)) and Durable async runs ([\#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408))
are complete, **do not automatically add another feature family.** Hold an
explicit **expansion checkpoint** to re-evaluate toward v1.0 rather than
continuing into:

```text
Event Grid
Storage Queue
Timer / cron
Webhooks
MCP server
A2A server
LangGraph BaseStore API clone
full LangGraph Platform parity
new checkpointer families without demonstrated demand
```

New feature proposals past this checkpoint should require concrete evidence:
repeated external user requests, GitHub issue/community demand, cookbook usage
proving a real scenario, or a clear gap that cannot be solved cleanly in
user-land or a sibling package.

## Stability & expansion checkpoint

After the expansion checkpoint, hold a stability review (a rolling milestone, not
a frozen v1.0 gate) and prioritize:

- [ ] Public API review / naming consistency, with each surface's stability tier declared.
- [ ] Dependency bounds and rolling minimum/latest compatibility lanes ([\#421](https://github.com/yeongseon/azure-functions-langgraph-python/issues/421)).
- [ ] Real-Azure release certification reliability.
- [ ] Security review of auth, message inputs, storage, serialization, and telemetry.
- [ ] Error contract consistency across native/Platform/streaming/trigger surfaces.
- [ ] Example smoke coverage and stale-example detection.
- [ ] Performance / cold-start baseline.
- [ ] Documentation consolidation so README stays short and examples/docs are canonical.
- [x] Rolling 0.x version-stability policy adopted ([\#426](https://github.com/yeongseon/azure-functions-langgraph-python/issues/426)).
- [ ] PyPI packaging/release gate hardening.
- [ ] External discoverability: official LangGraph docs/discussion/listing where appropriate.
- [ ] Measure adoption using PyPI download trend + external GitHub activity, while recognizing CI downloads inflate raw PyPI counts.

## Success criteria

The roadmap is successful when a new user can follow this path without
source-code archaeology:

```text
install
  -> deploy Azure OpenAI agent
  -> add conversation state
  -> add tools
  -> make state production-persistent
  -> run async graphs natively
  -> observe runs in Azure
  -> opt into true streaming
  -> consume Service Bus events if needed
  -> run long async lifecycles if needed
```

## Ordered checklist

- [x] [\#333](https://github.com/yeongseon/azure-functions-langgraph-python/issues/333) — release / OIDC certification pipeline *(P0, human-blocked)*
- [ ] [\#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350) — Azure Functions 2.x certification (P0)
- [x] [\#421](https://github.com/yeongseon/azure-functions-langgraph-python/issues/421) — LangGraph 1.2.x / sdk 0.4.x certification + rolling window (P0)
- [x] [\#402](https://github.com/yeongseon/azure-functions-langgraph-python/issues/402) — Azure OpenAI example
- [x] [\#403](https://github.com/yeongseon/azure-functions-langgraph-python/issues/403) — conversation memory example
- [x] [\#404](https://github.com/yeongseon/azure-functions-langgraph-python/issues/404) — tool-calling example
- [x] [\#405](https://github.com/yeongseon/azure-functions-langgraph-python/issues/405) — production persistent agent example
- [x] [\#422](https://github.com/yeongseon/azure-functions-langgraph-python/issues/422) — native async ainvoke/astream (P1)
- [x] [\#423](https://github.com/yeongseon/azure-functions-langgraph-python/issues/423) — `version="v2"` pass-through (P1)
- [x] [\#424](https://github.com/yeongseon/azure-functions-langgraph-python/issues/424) — production example thread-lock wiring + DESIGN.md refresh (P1)
- [x] [\#425](https://github.com/yeongseon/azure-functions-langgraph-python/issues/425) — run-lifecycle observer contract / #407a (P2)
- [x] [\#407](https://github.com/yeongseon/azure-functions-langgraph-python/issues/407) — Application Insights / OpenTelemetry integration / #407b (P2)
- [x] [\#406](https://github.com/yeongseon/azure-functions-langgraph-python/issues/406) — true HTTP streaming (P2)
- [x] [\#409](https://github.com/yeongseon/azure-functions-langgraph-python/issues/409) — Service Bus trigger (P2)
- [ ] [\#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408) — Durable async run lifecycle (P2, deferred)
- [x] [\#426](https://github.com/yeongseon/azure-functions-langgraph-python/issues/426) — rolling 0.x version-stability policy (P2)
- [ ] Expansion checkpoint → v1.0 stabilization / adoption phase; no automatic feature expansion

## Related completed work

- #144 — earlier infrastructure-oriented example expansion
- #378 — true-streaming architecture/design tracker
- #339 — core adapter vs experimental Platform compatibility boundary

## Priority

**Roadmap priority: P1.** Execution priority is defined per phase above; Phase 0
([\#333](https://github.com/yeongseon/azure-functions-langgraph-python/issues/333), [\#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350), [\#421](https://github.com/yeongseon/azure-functions-langgraph-python/issues/421)) is P0.

