# Deployment distribution boundaries — proposal

**Status: proposal for maintainer decision, not accepted packaging or support policy.**
Refs [#1006](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1006).
Publishing or merging this memo does not select a distribution boundary, announce
deprecation, promote a stability tier or approve the Durable proposals in
[#984](https://github.com/yeongseon/azure-functions-langgraph-python/issues/984).

## Recommendation and acceptance audit

**Propose option A: retain one `azure-functions-langgraph` distribution, existing
extras, lazy imports and public paths.** Optional SDK dependencies are already
isolated, while the integration adapters share execution, locking and registration
seams. The available evidence does not establish savings that justify new package
ownership, compatibility matrices and publishing/certification paths today.

| Issue checklist area | Evidence available | Remaining work or decision |
| --- | --- | --- |
| Clean core/extra installs, import cost, dependency overlap and release coupling; compare boundaries | The owner's [measurement comment](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1006#issuecomment-7167709498) reports these measurements; this memo preserves their provenance and compares A/B/C against current code. | Historical measurements were not repeated on the current revision. Prototype split-wheel savings, combined-extra satisfiability and controlled cold-start comparisons remain unmeasured; do not infer them from the existing installs. |
| Decision, migration cost, public imports, version compatibility, official examples, revisit conditions and Durable relationship | The proposal and trade-offs below cover these documentation topics. | The maintainer must explicitly accept or amend the packaging choice and revisit conditions. Durable maintenance/deprecation/investment and future authorization policy remain separate decisions under #984. |

Neither checklist item is marked complete here: the reported measurement work can
inform the audit, but documenting a recommendation is not a binding decision.
No matching packaging memo was tracked at the reviewed `origin/main` snapshot;
PR #1026 delivered SDK adoption guidance, not a distribution decision.

## Evidence provenance and limits

Current source review is bound to
[`8f63a6c5277dcc6fb73836daabee7ec26c8c74b8`](https://github.com/yeongseon/azure-functions-langgraph-python/tree/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8).
The measurements below are **reported historical evidence**, attributed to the
owner's 2026-11-25 comment at `284e7f9`, not new experiments performed for this
memo. That comment refers to a detailed memo, but does not link its raw logs or
exact command record. This page does not claim independent reproduction or invent
missing data. Re-measure a candidate before using these numbers as a deployment
budget or as evidence of current resolver behavior.

### Reported clean-install measurements

The owner reports clean CPython 3.11.15 environments installing a worktree-built
package: core plus each of the 12 extras separately. Size is logical
`site-packages` growth above the pip/setuptools seed, including bytecode and
metadata. Each successful install was timed once on a shared six-core machine
under load. Installation times are observations, not comparative benchmarks.

| Install | Net MiB | Distributions above seed | Install seconds (n=1) |
| --- | ---: | ---: | ---: |
| Core | 73.32 | 42 | 37.03 |
| + `azure-blob` | 97.37 | 48 | 40.84 |
| + `azure-table` | 79.12 | 48 | 21.79 |
| + `azure-identity` | 93.80 | 50 | 29.02 |
| + `postgres` | 86.23 | 46 | 22.56 |
| + `sqlite` | 73.98 | 45 | 34.35 |
| + `cosmos` | 97.96 | 52 | 21.85 |
| + `otel` | 73.74 | 43 | 19.56 |
| + `streaming` | 77.80 | 50 | 27.23 |
| + `durable` | 91.18 | 59 | 23.36 |
| + `openapi` | 74.19 | 43 | 16.92 |
| + `dev` | 217.82 | 127 | 56.38 |
| + `docs` | 149.19 | 67 | 25.37 |

All 13 reported `pip check` runs passed; each extra retained the same core
dependency versions. Core comprised the adapter plus 41 dependencies. Cosmos
included all eight incremental Identity distributions; Blob and Identity shared
four. Closures and sizes are **non-additive**. Separate extra installations do
not prove arbitrary combinations, dependency-floor compatibility or Python
3.12–3.14 resolution. `dev` and `docs` are tooling extras, not deployment needs.

The adapter reportedly occupied about 1.38 MiB, including distribution metadata.
Selected Experimental files occupied about 0.63 MiB, approximately 0.85% of core
install growth. Their SDK dependencies were already optional. This is a selected
file estimate, not a measured split-wheel reduction; the exact selection is not
enumerated in the comment. LangGraph brought `langgraph-sdk` transitively, so
moving Platform files alone would not remove that dependency from that install.

### Reported import measurements

Each reported statement ran in seven fresh Python processes with `-X importtime`.
Core root-import wall median was 106 ms (MAD 22 ms, range 71–152); importing
`LangGraphApp` was 1,015 ms (MAD 299 ms, range 596–1,876). Other environments
reported root medians of 22–72 ms and app medians of 367–792 ms. Temporal load and
uncontrolled filesystem caches confound comparisons: these are **not Azure
cold-start budgets or evidence that installing extras improves performance**.
The reported module-load probe confirmed root laziness and absence of selected
Experimental modules on the app import path.

The initial development installation exceeded its parent timeout; a fresh retry
completed. An incorrect Cosmos import path failed seven times and the corrected
path succeeded seven times. Neither is reported as a package defect. Import
optimization tracked in #940 is separate; this memo does not implement that work.

### Reported release coupling

Between May 25 and the measurement snapshot, the owner reports 436 commits and
22 reachable version tags. Platform changed in 49 commits, Durable in 16,
streaming in 26, stores in 16 and triggers in nine. Their union was 83 commits:
64 did not touch any of five stable-functional public-module proxy paths; 19
co-touched at least one. Eight tag ranges contained Experimental changes without
functional-core-proxy changes; version bumps were excluded from the proxy.

This suggests potential cadence isolation, **not eight proven avoidable
releases**: the proxy does not cover every Beta/private/dependency/security
change. Reachable tags are history boundaries, not evidence of PyPI publication.
The comment does not enumerate the five paths or per-tag counts, so the summary
is insufficient to independently reproduce the classification. Current release
ownership remains governed by the [release process](release_process.md).

## Current code explains the boundary costs

The following source links are pinned to the reviewed snapshot, rather than the
older measurement revision:

- [`pyproject.toml`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/pyproject.toml)
  declares Azure Functions, LangGraph, LangChain Core and Pydantic as runtime
  dependencies, with 12 extras. Durable, streaming and storage SDKs are not core
  requirements. A native Functions deployment still needs the Azure host package.
- [`__init__.py`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/src/azure_functions_langgraph/__init__.py#L49-L135)
  resolves root public symbols lazily and names missing extras. Distribution
  separation is therefore not required merely to avoid importing a feature.
- [`app.py`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/src/azure_functions_langgraph/app.py#L743-L821)
  always registers native routes; Platform and Durable registration is opt-in.
  Their dependency objects receive the live graph registry, scoped lock and
  observer. Durable also changes the constructed host to `DFApp`.
- The [execution planning matrix](architecture.md#planning-stage-matrix) and
  [cross-surface lock coordination](architecture.md#cross-surface-lock-coordination)
  explain what is shared and what must remain transport-specific. A split needs
  owned interfaces for registration, topology, execution configuration, observer
  events and checkpoint-writer exclusion; moving directories is insufficient.
- The [public API inventory](api-inventory.md),
  [API boundary policy](api-boundary.md) and
  [finite Platform matrix](https://github.com/yeongseon/azure-functions-langgraph-python/blob/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/COMPATIBILITY.md)
  define existing paths and per-surface contracts. Package-level Beta is not
  equivalent to every integration being Stable or production-certified.

## Options and migration implications

| Concern | A: current distribution + extras (recommended proposal) | B: Experimental companion distribution(s) | C: core / Platform / Azure distributions |
| --- | --- | --- | --- |
| Benefit supported by evidence | Existing optional dependencies and lazy imports provide isolation without new package seams. | Potential cadence separation; selected file savings are small and actual split-wheel savings unmeasured. | Potential reuse by a host-neutral consumer; no such independent consumer is demonstrated here. |
| Migration cost | None for current imports, requirements or app registration. | Moderate-to-high: extract shared interfaces and optional registration, retain a facade/shims, update requirements and absent-feature errors. | Highest: establish a genuinely host-neutral, acyclic core plus Azure/Platform adapters and an existing-import facade. Functions users still install Azure + core. |
| Public imports | Keep root symbols and documented submodule imports, including streaming, stores, checkpointers and locks. | Preserve existing paths during a classified migration; decide companion installation, shim lifetime and errors explicitly. Namespace ownership must avoid conflicting files between wheels. | Preserve old facade paths first; any eventual import/default/behavior incompatibility needs contract classification and migration, not an incidental rename. |
| Versions | One package version; existing dependency windows and CI remain authoritative. | Exact core pins constrain independent releases. Version ranges need core/companion skew tests, dependency bounds and rollback combinations. | Own core↔Azure, core↔Platform and composed compatibility ranges. Two supported versions of three packages give an illustrative eight-combination upper bound before runtime variants, not an observed matrix. |
| Releases | One Release Please version decision and candidate artifact with current gates. | Own additional package releases, publishing identities and candidate composition/certification; no weaker gates. | Own multiple versions/artifacts and composed release verification, with explicit maintainers and test budget. |
| Examples | Retain current native official graph and optional-feature suites. | Test core without companions and integrations with companion candidate wheels. | Test official native graph through Azure + core, plus Platform/other supported compositions and old facade imports. |

The [official LangGraph example](https://github.com/yeongseon/azure-functions-langgraph-python/tree/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/examples/official_langgraph_basic)
preserves upstream graph snippets. Its
[three smoke tests](https://github.com/yeongseon/azure-functions-langgraph-python/blob/8f63a6c5277dcc6fb73836daabee7ec26c8c74b8/tests/test_official_langgraph_basic_example.py)
check verbatim snippets, native result parity and registered HTTP-handler parity.
The owner reported all three passing against an installed package. The graph's
`langchain` dependency is example-specific; it needs neither Platform nor Durable.
Any split proposal must preserve the graph verbatim and exercise registration
against installed candidate wheels, not just source imports. Optional SDK,
streaming, Durable and storage examples also need their composed-candidate tests.
Existing cloud evidence does not certify hypothetical split packages; this memo
adds no Azure deployment, cold-start measurement or certification.

## Conditions to revisit the proposal

Reconsider **B for a named integration** when repeatable deployment-budget or
resolver evidence shows material cost **unavoidable with extras/laziness**, or
actual independent urgent-release demand justifies a new release boundary.
Before selecting it, require:

1. Identified adopters and an owned compatibility seam with a maintenance/test budget.
2. Prototype candidate wheels measuring clean core, individual and intended
   combined-extra installs, dependency overlap and controlled import/host impact
   on relevant Python versions; retain commands, versions and raw results.
3. A release-history analysis that accounts for functional, Beta, dependency and
   security coupling rather than treating every untouched proxy path as independence.
4. Public import/registration compatibility, version-skew coverage, classified
   migration, rollback and composed-artifact release/example verification.

Consider **C only after** demonstrating independent host-neutral consumers and
an acyclic reusable core, in addition to the B requirements. No automatic numeric
threshold or split deadline is accepted by this proposal.

## Durable is a separate support decision

The [#984 acceptance audit](architecture.md#acceptance-audit-after-pr-1002) records
that publication of PR #1002 adopted none of its support proposals. Current
Durable create/poll/cancel routes use app-wide host authentication, not the native
`authorize` callback; Activity redelivery, cancellation and checkpoints retain
their documented limitations. Packaging cannot repair ownership, deduplication,
recovery or checkpoint fencing guarantees.

This recommendation does not select Durable maintenance expectations,
deprecation/investment conditions or future authorization requirements. Preserve
existing callers and compatible in-flight history pending the explicit #984
decision and [app-owned migration design](production-guide.md#app-owned-dfapp-migration-decision-draft).

**Decision requested:** the maintainer should explicitly accept or amend option A
and the revisit conditions on #1006. Record Durable policy separately on #984.
Until then, both remain proposals and existing packaging/support contracts stand.
