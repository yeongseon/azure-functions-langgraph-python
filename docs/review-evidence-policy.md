# Review Evidence Policy (advisory)

> **Status: advisory, not server-enforced.** Nothing in this document is a required status
> check. Branch protection still gates `main` only on the five required contexts (`bandit`,
> `semgrep`, `Analyze`, `PR title`, `ci-required`) and conversation resolution — see
> [AGENTS.md "Merge Policy"](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/AGENTS.md#merge-policy).
> This policy defines what review *evidence* a maintainer should record before merging; it does
> not add a required check, and must not be treated as one until a trusted verdict publisher is
> configured and verified in this repository (see "Rollout" below).

This repository runs with zero required native GitHub approvals because the sole maintainer
cannot approve their own PR. Independent review still happens — GitHub Copilot code review, or
a human — but prior to this policy nothing recorded *who* reviewed *which commit*, *what* was
found, or whether findings were resolved before merge. This document defines that evidence
record. It does **not** reopen the zero-approval decision (it stands) and does **not** replace CI.

## Roles

- **Implementer** — the person or agent that authored the change (may be the maintainer).
- **Independent reviewer** — a review source distinct from the implementer: GitHub Copilot code
  review, or a human maintainer/contributor who did not author the diff. A review is only
  independent evidence if it evaluates the PR's current head commit; a thread opened against an
  earlier commit is stale once the diff changes (see "SHA binding" below).

Implementer-managed helper reviews (e.g. an agent re-running its own linter and calling the
result a "review") are supplemental context, not independent review evidence.

## SHA binding (why this matters)

A review is evidence for the commit it evaluated — never for the PR as a whole:

- Use the **formal review's reviewed commit** (`reviews[].commit_id` via the GitHub API), not an
  inline comment's current `commit_id`. GitHub re-targets an inline comment's `commit_id` to the
  latest push even when the comment's `original_commit_id` still names the commit it was actually
  left against; only `original_commit_id` is trustworthy provenance for *when* the comment was
  made.
- A reply to an existing review thread — including one authored by the PR's own implementer — is
  not a new independent review of the diff, even when it is the most recent activity on the final
  head.
- Any push after a review invalidates that review's evidence for the new head. A green CI
  aggregate or a resolved thread from a stale commit does not stand in for re-review of the
  current one.

## Review effort

Seek additional review when risk or unresolved findings justify it, without a mandatory number of AI review rounds. Maintain explicit human review for high-risk changes.

## What to record per PR

For routine PRs, record a short summary, key changes, actual validation, and related issues. State significant findings and blockers when relevant. Do not require risk tiers, evidence placeholders, or agent process logs on ordinary PRs.

For changes in the high-risk R3 scope below, additionally record the reviewed commit, material findings, and explicit maintainer authorization. This evidence can be a short dedicated section; AI review does not itself establish human approval.

- For runtime/storage paths specifically: the actual ownership contract exercised — internal
  owner tokens and expiry surviving copies/serialization while remaining excluded from public
  responses; stale-owner renewal/release rejection; per-thread exclusion across the affected
  native/Platform/trigger routes and state mutations; checkpoint/pending-write round trips and
  subsequent resumed state — distinguishing thread metadata, graph checkpoints, and application
  user authorization. Cancellation and iterator cleanup must be assessed against when graph work
  actually stops (including in-flight acquisition or worker execution), not just HTTP/task
  completion.
- For streaming changes specifically: meaningful graph-data output, error frames, and the
  expected terminal outcome. HTTP 200, any JSON `data:` line, or an `end` frame alone does not
  establish successful execution. Measure incremental arrival before completion only when
  claiming true streaming; preserve the classic buffered contract. Record actual
  LangGraph/SDK/backend versions, executed operations, and skips — in-memory fakes or SQLite do
  not prove Postgres, Cosmos, Azure Blob/Table, or cross-instance behavior. Use the existing
  #573/#644 storage/certification follow-ups for their own implementation scope rather than
  re-litigating them here.
- For packaging/release changes specifically: the reviewed source SHA, tested wheel and installed
  version/import location, any separately built certification wheel, and the observed deployed
  artifact/version and Functions plan/runtime — distinguishing emulator/local execution from real
  Azure certification and publication. A matching source SHA or a healthy host alone does not
  establish these claims. Keep the existing release-guidance work (#645) separate.

External contributors may use ordinary human review without subscriptions or AI tooling.

## R3 scope

Treat the following as higher-risk ("R3") and require an explicit human maintainer authorization
record tied to the reviewed commit, in addition to any automated review:

- Workflow permissions (`permissions:` blocks, `GITHUB_TOKEN` scope, secrets usage).
- Release/publishing configuration (`publish-pypi.yml`, `release-please.yml`, PyPI Trusted
  Publishing config, `e2e-azure.yml`'s candidate-wheel bundling and identity gate).
- Authorization and checkpoint deserialization/credential boundaries (`authorization.py`,
  checkpointer `serde` boundaries under `src/azure_functions_langgraph/checkpointers/`).
- Checkpoint retention or state mutation (`delete_thread`, `delete_old_checkpoints`,
  `delete_checkpoints_before`, `threads_state_update`).
- Ownership/fencing/cancellation/retry behavior that can discard state or in-flight work
  (`locks/`, `platform/_runs.py`, `platform/_threads.py`, `streaming.py`, `durable/_activity.py`).
- Review policy or evaluator configuration itself — changes to this document, reviewer
  allowlists, or `CODEOWNERS` if introduced.
- Policy-bearing files generally: `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md`,
  `docs/agent-playbook.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/release_process.md`,
  relevant security/ownership policy in `docs/security.md` and `docs/production-guide.md`,
  `DESIGN.md`, this document.

An account token, an automatically posted comment, the merge actor, or a successful CI run does
not by itself prove a human authorized an R3 change — record who actually made the call, on which
commit, and why.

## Exceptions (explicit policy cases)

These are documented **exceptions**, not instances of independent review — they remain visible as
exceptions in PR history rather than being recorded as if a review had occurred:

- **Release Please** PRs: version/changelog-only; mechanical, not independently reviewed.
- **Dependabot `pip` patch/minor**: auto-merged on green required CI per
  [AGENTS.md "Merge Policy"](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/AGENTS.md#merge-policy); the full test matrix is the evidence.
- **GitHub Actions SHA updates**: never auto-merged; require a human diff check confirming the
  pinned SHA matches its claimed tag (see AGENTS.md). This human check *is* the review evidence —
  record the SHA/tag comparison performed.
- **Forks**: external PRs are reviewed by a maintainer before merge; Copilot review may be
  unavailable or restricted on fork PRs depending on repository settings — record "unavailable"
  explicitly rather than silently skipping the field.
- **Reviewer outages / emergencies**: if Copilot review is unavailable (quota, outage, provider
  error), record the unavailability and fall back to human review before merge. This is never a
  reason to merge with the review field left blank.

## Provenance pitfalls (do not accept as evidence)

- A bot `COMMENTED` or `APPROVED` state alone does not prove independent human authorization —
  check reviewer identity/type, not just the review state.
- A shared GitHub Actions app ID is not sufficient to identify the trusted evaluator: any
  PR-controlled workflow can emit a result under the same app ID. Verdict-publisher identity must
  be resolved before any such result is treated as enforced evidence (see "Rollout").
- An unknown or missing response must never default to "approved" — treat it as "evidence absent."
- A resolved review thread does not establish the findings were addressed on the *current* head
  if the thread's reviewed commit predates later pushes.

## Rollout

This policy begins **advisory-only**. Before any of it becomes a required, server-enforced check:

1. The verdict-publisher's identity must be independently resolved (not just a shared app ID) and
   documented.
2. The exact required-check binding (context name + trusted publisher) must be configured and
   verified against this repository's branch protection.
3. Administrator/emergency bypass boundaries for that check must be documented alongside the
   existing publish-workflow bypass rules in AGENTS.md.

Until all three are done, nothing in this document overrides or supersedes the required CI
contexts and conversation-resolution policy already enforced on `main`.
