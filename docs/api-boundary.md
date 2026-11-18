# API boundary and migration policy

> **Status: proposal for maintainer approval (issues #878 and #930).** This page
> records an inventory-backed assessment, and section 8 records a decision about an
> entrypoint facade. It changes no import path, export or runtime
> behavior. Nothing here is a commitment to 1.0; the package stays on the rolling
> 0.x policy in the [README stability table](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/README.md#stability--rolling-0x-per-surface).

The machine-generated facts (exports, signatures, routes, extras, environment
variables, in-repo consumers) live in the [public API inventory](api-inventory.md)
([JSON](api-inventory.json)). Regenerate with
`python tools/public_api_inventory.py --write`; `--check` fails when the
committed files are stale and also runs in the test suite.

## 1. Tier matrix

Tiers are assigned **per surface**, not per package, and follow the README table.
"Stable-intent Core" means the maintainers intend to preserve supported calls and
require explicit breaking-change classification and migration guidance
([CONTRIBUTING.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CONTRIBUTING.md#breaking-change-classification));
it is not a 1.0 guarantee.

| Surface | Import paths / routes | Tier | Basis | Extra |
| --- | --- | --- | --- | --- |
| App construction, `register`, `function_app`, auth levels | `LangGraphApp`, `get_langgraph_metadata` (`app`) | Stable-intent Core | README | runtime deps |
| Native HTTP routes | `POST /graphs/{name}/invoke`, `POST /graphs/{name}/stream` (buffered SSE), `GET /graphs/{name}/threads/{thread_id}/state`, `GET /health`, `/health/details`, `/health/ready` | Stable-intent Core | README | none |
| Request/response models, metadata dataclasses | `contracts` | Stable-intent Core | README | none |
| Graph protocols | `protocols` (`LangGraphLike`, ...) | Stable-intent Core | README | none |
| `RunObserver` contract | `observability`, root observer symbols | Beta | README | none |
| OpenTelemetry observer | `observability_otel`, `OTelRunObserver` | Beta | README | `otel` |
| Checkpoint backends | `checkpointers.*` | Beta | README | `azure-blob`, `postgres`, `sqlite`, `cosmos` |
| Thread locks, ownership guard | `locks.*` | Beta | README | `azure-blob` for the lease lock |
| Authorization hook | `authorization` | Beta | provisional | none |
| OpenAPI bridge | `openapi` | Beta | provisional | `openapi` |
| Platform SDK compatibility | `platform.*`, `platform_compat=True`, Platform routes | Experimental | README | none (`langgraph-sdk` is a client-side dependency) |
| Platform thread store (Azure Table) | `stores.*` | Experimental | provisional | `azure-table` |
| True HTTP streaming | `streaming`, `streaming_authorization`, `StreamingLangGraphApp` | Experimental | README | `streaming` |
| Service Bus trigger | `triggers.*`, `default_message_mapper`, `ServiceBusMessageLike`, `ThreadContentionError` | Experimental | README | none (`azure-functions` trigger binding) |
| Durable async-run lifecycle | `durable.*`, `async_runs="durable"`, Durable routes | Experimental | README | `durable` |
| Private modules | any path component starting with `_` | Internal | n/a | n/a |

Notes:

- `LangGraphApp` is Stable-intent Core, but its **opt-in parameters**
  `platform_compat` and `async_runs="durable"` enable Experimental surfaces. The
  tier belongs to the surface, not to the class that hosts the switch.
- Rows marked `provisional` are not named in the README table. They were placed
  by analogy and need maintainer confirmation (see section 7).
- The Core classification of `contracts` and `protocols` follows from them being
  the types of the Core calls and routes; the README table does not list them
  separately.

## 2. Native HTTP contracts vs Platform SDK compatibility

| | Native HTTP | Platform compatibility |
| --- | --- | --- |
| Enabled by | default | `LangGraphApp(platform_compat=True)` |
| Routes | `/graphs/{name}/...`, `/health*` | `/assistants/*`, `/threads/*`, `/runs/*` |
| Tier | Stable-intent Core | Experimental, explicitly partial |
| Promise | Request/response shapes, status codes and auth levels documented for the native routes | Finite, tested workflows with the supported `langgraph-sdk` range (see [COMPATIBILITY.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md#platform-compatibility-layer)); unsupported controls return an explicit 501 |
| SDK parity | not applicable | **Not promised.** No general LangGraph Platform or SDK parity; endpoints may change between minor releases |
| Infrastructure | none beyond Azure Functions | process-local thread records unless a shared store is configured |

Durable lifecycle routes (`POST /graphs/{name}/runs`, `GET /runs/{run_id}`,
`POST /runs/{run_id}/cancel`) and the `StreamingLangGraphApp` routes are separate
Experimental surfaces. They are not part of the native Core contract even though
some paths look alike.

## 3. Does an `experimental` namespace help?

Options considered:

| Option | What changes | Assessment |
| --- | --- | --- |
| **A. No move (recommended)** | Keep every import path. Publish the inventory, the tier matrix and a deprecation policy; guard the inventory with a test. | Zero consumer churn; closes the discoverability gap with documentation. |
| B. `azure_functions_langgraph.experimental` with shims | Relocate Platform/Durable/Streaming/Service Bus symbols; old paths become warning shims. | Adds a second path for every symbol, shim code to maintain and warnings for every current consumer, for no runtime benefit. |
| C. Root-only tidy | Stop re-exporting the four Experimental symbols from the root package. | Removes supported imports (breaking under the CONTRIBUTING classification) to fix a labelling problem. |

Reasoning for A:

1. **A move does not reduce runtime coupling.** The inventory shows Experimental
   code is already isolated in its own modules (`platform/`, `durable/`,
   `triggers/`, `streaming.py`, `stores/`) and is imported lazily: `LangGraphApp`
   imports Platform and Durable code inside functions that run only when the
   corresponding option is enabled, and optional extras are imported at call
   time. A namespace would be a rename of already-separated code.
2. **The affected surface is small.** Of the 30 root exports only four are
   Experimental (`StreamingLangGraphApp`, `ServiceBusMessageLike`,
   `ThreadContentionError`, `default_message_mapper`); the rest of the Experimental
   surface is already reached through subpackage paths that carry their own name
   (`platform`, `durable`, `triggers`, `streaming`).
3. **Discoverability is addressed more cheaply.** The tier is declared in the
   README, this page and the generated inventory, and enforced by a test; the
   [stability banner](adapter-reference.md) is already on the adapter reference.
4. **Churn is real, benefit is not.** Examples, tests, docs, the translated
   READMEs and downstream code reference the current paths. Under rolling 0.x,
   Experimental already means "may change between minors", so the relabelling
   would be duplicated by a path change nobody asked for.
5. **Shim cost is non-trivial.** A shim must keep lazy-import and
   missing-extra error behavior identical, preserve object identity, and
   emit warnings at a precisely defined moment (section 4). That is permanent
   maintenance in exchange for cosmetics.

**Recommendation: adopt option A.** No follow-up issue is justified today, and
none is filed. Revisit when one of these concrete triggers occurs:

- an Experimental surface needs an incompatible API change and a new path would
  carry a clean replacement (then use the standard policy below, not a blanket
  namespace);
- an Experimental surface graduates to Beta/Core and its path should reflect it;
- a measured import-time or dependency problem that only a module boundary fixes.

If a real move is ever approved it should be proposed per surface, in its own
focused issue, using the policy and migration-table format below.

## 4. Proposed migration and deprecation policy

Applies to relocations, renames, removals and signature/behavior changes of a
supported surface. Compatibility classification still follows CONTRIBUTING; the
`!` marker is used only when a supported usage becomes incompatible.

| Tier | Policy |
| --- | --- |
| Stable-intent Core | Provide the replacement and a deprecation warning for at least one minor release before removal, where technically feasible. Removal is classified breaking, listed in the changelog and in the migration table. |
| Beta | Same as Core, but the notice period may be shortened when a settled shape needs a refinement; the change is still documented and listed in the migration table. |
| Experimental | May change between minor releases. A warning and migration note are provided on a best-effort basis; there is no notice-period guarantee. Removal is still recorded in the changelog. |
| Internal (`_` modules) | No promise. May change at any time. |
| Security or unsafe behavior | Exception to every row above: a behavior that is unsafe or exploitable may be changed or removed in the same release that fixes it, with no prior warning period. The release notes and migration table must state the change and the reason; breaking classification still follows verified behavior, not motivation. |

### Warning mechanics

These rules define how a future shim must behave; no shim exists today.

- **Category and message.** `DeprecationWarning`, naming the old path, the new
  path and the first release in which the old path may be removed.
- **When it fires.** At the point the old name is *used*, never at
  `import azure_functions_langgraph`. For a relocated import path the warning is
  raised from module-level `__getattr__` (PEP 562) on first attribute access, so
  `from old.path import Name` warns and merely importing the package does not.
  For a deprecated argument or behavior it is raised when the argument is
  supplied or the behavior is taken, with `stacklevel=2` so the warning points to
  the caller. Importing the *new* path never warns.
- **No import-time side effects.** The shim must not import an optional extra or
  any module the old path did not already import. Missing-extra `ImportError`
  messages stay identical.
- **Identity.** The old name returns the same object as the new one
  (`old is new`), so `isinstance` checks and type annotations keep working.
- **Visibility.** Python hides `DeprecationWarning` outside `__main__` by default;
  the release notes for a deprecation show `python -W default::DeprecationWarning`,
  and the PR that adds a shim adds a test that in-repo code no longer triggers it
  (the repository has no global `filterwarnings = error` setting today).
- **Timeline.** The release that adds the warning is `feat`/`fix` with the
  deprecation described in the changelog; removal happens no earlier than the
  next minor release and is marked breaking when it removes a supported usage.
  `__all__` is not trimmed until removal.

### Migration table

No path changes are proposed. The table is the template to use when one is
approved and records today's decision for every affected symbol group.

| Surface | Current path (supported) | Candidate path | Decision | Warning | Earliest removal |
| --- | --- | --- | --- | --- | --- |
| `StreamingLangGraphApp` | `azure_functions_langgraph` and `.streaming` | `.experimental.streaming` (not adopted) | No move | none | n/a |
| Service Bus symbols (`default_message_mapper`, `ServiceBusMessageLike`, `ThreadContentionError`) | `azure_functions_langgraph` and `.triggers.service_bus` | `.experimental.triggers` (not adopted) | No move | none | n/a |
| Platform compatibility | `.platform.*`, `platform_compat=True` | `.experimental.platform` (not adopted) | No move | none | n/a |
| Durable lifecycle | `.durable.*`, `async_runs="durable"` | `.experimental.durable` (not adopted) | No move | none | n/a |
| Platform thread store | `.stores.azure_table` | none | No move | none | n/a |
| Core and Beta surfaces | see inventory | none | No move | none | n/a |

## 5. Guarantees enforced by tests

`tests/test_public_api_inventory.py` pins, without importing optional extras
for the scan itself:

- the committed inventory equals a fresh scan, and `--check` exits non-zero when
  it is stale;
- `__all__` and the lazy-export table agree, and every export is classified into
  a tier;
- positive checks: every Core/Beta root export that needs no extra resolves
  lazily; negative checks: unknown names raise `AttributeError`, third-party
  names are not re-exported, and a missing extra yields the install-hint
  `ImportError`;
- importing the package imports none of the optional extras, `azure.functions`
  or `langgraph`, and the scanner itself does not import the package.

## 6. Out of scope

- Moving, renaming or removing any import or behavior.
- A CI snapshot of the API contract (the `--check` mode and its test are the
  starting point; wiring it into workflows is separate work).
- Shared internal execution planning (#861), docs restructuring (#863) and the
  authorization work (#854) are related but independent.
- Contributor-reserved examples (#817-#826).

## 7. Decisions needed from a maintainer

1. Approve the staged decision: **no move now**, publish the inventory, adopt the
   policy in section 4. (This is the acceptance item that cannot be self-approved
   in a pull request.)
2. Confirm or change the `provisional` tiers: `authorization`, `openapi`,
   `stores`, `streaming_authorization`.
3. Confirm that a separate follow-up is not needed (none is recommended).
4. Decide on the entrypoint facade of section 8. The proposal is **no facade now**,
   with the triggers listed there for revisiting it.

## 8. A thin entrypoint facade (proposal, #930)

[#913](https://github.com/yeongseon/azure-functions-langgraph-python/issues/913)
gives `app = LangGraphApp(graph)` as an illustrative target only and asks whether a thin
entrypoint facade is justified before any signature is implemented. This section records
what was verified and what is not known, so the decision can be made on evidence. It adds
no export, signature or behavior.

### What the host requires

Observed with Azure Functions Core Tools 4.8.0, Python 3.11.15 and
`azure-functions-langgraph` 0.13.0 from PyPI, in a `func init` project with no storage
emulator. These are local runs; no Azure-hosted behavior is claimed.

| Top level of `function_app.py` | Result |
| --- | --- |
| `app = langgraph_app.function_app` (today) | the host registers `aflg_<graph>_invoke`, `_state` and `_stream` plus `aflg_health` and `aflg_health_details`; `GET /api/health` answers 200 |
| `app = LangGraphApp()` and `app.register(...)`, without `.function_app` | `Worker failed to index functions`, `ValueError: Could not find top level function app instances in function_app.py.` and `No job functions found`; no route exists |
| the scaffold's `app = func.FunctionApp()` next to the library's function app | `ValueError: More than one FunctionApp or other top level function app instances are defined.`; no route exists |

The worker needs exactly one top-level function app instance, and a `LangGraphApp` object
is not one. The illustrative `app = LangGraphApp(graph)` therefore cannot work today, and
any shorter entrypoint has to either return a function app (option B) or make
`LangGraphApp` itself one (option C).

### What the integration costs today

The [first-run baseline](onboarding-baseline.md) records it for
`examples/simple_agent/function_app.py`: 9 code lines and 5 top-level statements (two
imports, construct, register, export) with no constructor argument. The other measured
first-run steps are not integration code: 6 command lines to the first local response, 5
files and 4 requirements. Of the 28 example apps, 27 host a graph through
`langgraph_app.function_app`; the other is a timer utility.

### Options

| Option | What the user writes | What it saves against the baseline | Costs and unknowns |
| --- | --- | --- | --- |
| **A. No facade (proposed)** | unchanged: construct, register, export `.function_app` | nothing | nothing new; the two indexing failures above can be documented |
| B. A function that builds and returns the function app for one graph, for example `app = serve(graph, name="simple_agent")` (a hypothetical name) | two imports and one call | 3 top-level statements instead of 5 | a new public name in the Stable-intent core, which exports 31 names today; a second way to do the same thing that the examples, READMEs and tests must choose between; it forwards only some of the 17 constructor parameters and the per-graph `register` options, so a user who needs a second graph, a thread lock, an observer or `platform_compat` rewrites the entrypoint, unless it forwards everything, which makes it a second spelling of the same API |
| C. `LangGraphApp` is itself the function app the worker finds | `app = LangGraphApp(...)` | construct, register and export collapse into one construction | **not validated.** `function_app` is a cached property that `register`, `register_service_bus` and the `thread_store` setter invalidate, so today a change followed by a new access builds a new `FunctionApp`, and registrations made after Platform or Durable routes were exported are checked against that exported registry (#854). The worker takes the top-level object once, so a discoverable `LangGraphApp` needs a defined moment at which routes are built and a defined behavior for registrations after it. This changes a Stable-intent class |

### Recommendation

**Option A: do not add a facade now.**

1. The saving is small and measured: at most 2 statements of 5 (option B), while the first-run
   steps the baseline measures lie mostly outside these lines (the clone, the virtual
   environment, the installation and the host start).
2. Option B adds permanent surface to the Stable-intent core and a second spelling of the
   same API, with a capability cliff for anything beyond a single graph.
3. Option C is the only way to make `app = LangGraphApp(...)` real, and it is the option
   whose lifecycle is not validated.
4. The failure modes observed above are cheaper to address by documentation, which needs no
   API change.

Revisit when:

- a measured, repeatable newcomer task (for example a timed walkthrough or recorded support
  questions) shows the construct, register and export statements as a dominant source of
  failures or time; or
- a prototype answers option C's lifecycle questions and the maintainers accept a change to
  the class.

### What was not validated

- Option C: what happens when the worker indexes the module, the identity of the function
  app across `register` calls, and the late-registration and topology checks.
- Option B: a signature, behavior with several graphs, and which constructor options it
  would forward.
- Any Azure-hosted behavior (Flex Consumption or Premium), and other Core Tools or
  `azure-functions` versions.
