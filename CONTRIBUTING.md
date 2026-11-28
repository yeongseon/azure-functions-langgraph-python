# Contributing Guide

We welcome contributions to the `azure-functions-langgraph` project.


## Contributor-reserved issues

Issues labeled `good first issue` or `help wanted`, and issues assigned to or actively claimed by an external contributor, are **reserved for outside human-led contributors** (including code prepared with AI tools under a contributor's direction and review).

- You may start an **unclaimed** issue and submit a PR without advance maintainer approval or GitHub Assignee permission. First check the issue, Assignees, discussion and open PRs. A comment expressing interest is encouraged but optional; coordinate rather than duplicate someone else's work.
- The owner, maintainer-operated coding agents, and automated backlog-clearing jobs must not take these issues away, implement competing solutions, silently change their labels/scope or close them. A generic instruction to clear the backlog is **not** an exception.
- Maintainers are available to answer questions, confirm assignments, run protected verification, review and merge contributor PRs. They may change a reservation only through a **documented, issue-specific decision**, after coordinating any active contributor handoff; claims are not automatically expired.
- See [AGENTS.md — Contributor-reserved issues](AGENTS.md#contributor-reserved-issues) for the full owner/agent workflow. Required CI, review and release rules remain unchanged.

## Branch Strategy

Use GitHub Flow and branch from `main`.

Recommended branch prefixes:

- `feat/` for new features
- `fix/` for bug fixes
- `docs/` for documentation-only changes
- `chore/` for tooling and maintenance
- `ci/` for workflow updates

## PR review outcomes

Use the same decision labels for every PR, whether human-authored or AI-assisted:

- **`APPROVE`** a reviewed, non-blocking change; optional suggestions can accompany approval instead of delaying it.
- **`REQUEST_CHANGES`** for an actionable must-fix defect, with the affected code and required correction clearly identified.
- **`COMMENT`** when asking a question or when a review is not yet decisive. The author cannot approve their own PR; never imply an AI review is an independent human approval.
- Code approval and GitHub Actions **run approval** are separate. `action_required`, pending, failed or stale required CI checks do not permit merge, even if a code review is approved. Confirm final-head CI, resolved blocking findings, applicable independent/security/integration requirements and the maintainer's merge decision before merging.

See [AGENTS.md — PR review outcomes](AGENTS.md#pr-review-outcomes-common-decision-rules). Existing repository-specific branch, release, security and required-review safeguards continue to apply.

## Pull Request Review and Merge Policy

Apply identical PR review criteria to human-only and AI-assisted work
(OpenCode, Codex, Claude Code, ChatGPT, Copilot, and others).
Implementation tool choice never adds a mandatory AI review route.

- Open a focused PR following [Issue or direct PR?](#issue-or-direct-pr), with
  relevant tests, docs and a truthful account of checks performed or omitted.
- Trust required **GitHub CI** checks at the final head for official
  automated validation. Running `make check-all` locally is recommended,
  not a separate merge condition; do not assume a specific runner model.
- Review correctness, tests, security, maintainability and compatibility.
  AI review outputs are advisory inputs, not approvals or CI substitutes.
- Resolve actionable findings; the responsible maintainer decides on
  merge after checking required CI and existing runtime, security,
  release and integration safeguards. Permission/publishing changes
  receive explicit independent scrutiny.

## Issue or direct PR?

A docs-only correction that only fixes attribution, a link target, a typo, formatting or a factual detail already verifiable against the code or the cited source, and that does not alter an official graph, a support/stability/compatibility claim, a public API, an example's behavior, release/CI/credential files, or work under a contributor-reserved issue, MAY go straight to a pull request without a prior issue; everything else needs an issue first.

Before either path, search [existing issues](https://github.com/yeongseon/azure-functions-langgraph-python/issues)
and [open PRs](https://github.com/yeongseon/azure-functions-langgraph-python/pulls)
for the same work. Inspect matching issues' labels, Assignees, comments and linked
PRs; follow [Contributor-reserved issues](#contributor-reserved-issues). A direct PR
is not a way to bypass a reservation or an active claim. If the scope is uncertain,
use the issue-first path and describe what needs deciding.

### Minimal steps for a small correction

1. Check the boundary above and search for existing work. No assignment request or
   advance maintainer permission is needed for an eligible, unclaimed correction.
2. Fork the repository if you do not have push access. Edit the documentation on a
   branch from current `main`; GitHub's web editor is sufficient for a one-line fix.
3. Verify the corrected text against the code or cited source and preview the diff.
   For site-page changes, run `make docs` when practical. Describe the source and
   checks actually performed, and any checks not run; a prose-only fix needs no new
   runtime test. Required GitHub CI and existing review/merge safeguards still apply.
4. Open a focused PR using the [Title Convention](#title-convention) and the PR
   template. In Related Issues, explain why the correction qualifies without a
   prior issue, or link existing related work. Use a Draft PR if unfinished.

**Trivial correction walkthrough:** a documentation link points to the wrong
upstream attribution page. After checking for reserved/claimed or duplicate work,
verify the intended page and attribution against the cited upstream source. On
your fork's branch, change only that link target, preview and check it, then open a
PR explaining the old and corrected targets and your verification. With no change
to the graph, behavior or claims, no prior issue is needed.

**Issue-first walkthrough:** you want to describe an Experimental surface as
Stable. Even if the edit is one word in Markdown, it changes a stability claim.
Search for existing work, inspect reservations and claims, then open or use an
issue describing the evidence, proposed scope and acceptance criteria under
[Issue Scope](#issue-scope). Coordinate any active contribution before editing;
link that issue in the eventual PR. The same issue-first path applies to changing
an official example's graph or behavior. Opening an issue is not a new approval
gate; existing review and merge requirements remain unchanged.

## Development Workflow

Choose the [issue or direct-PR path](#issue-or-direct-pr) first. For changes that
need a local checkout (rather than the small-correction web-editor path):

1. Create a branch from `main`.
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature-name
   ```
2. Write code and tests.
3. Run the local quality gate when practical; required GitHub CI is authoritative.
   ```bash
   make check-all
   ```
4. Push and create a pull request.
   ```bash
   git push origin feat/your-feature-name
   ```

## Project Commands

```bash
make format      # Format code with ruff
make lint        # Lint with ruff
make typecheck   # Type check with mypy
make test        # Run tests
make cov         # Run tests with coverage
make check-all   # Run the full local gate
```

`make test` runs the source metadata checks but explicitly skips the built
wheel/sdist metadata checks unless `METADATA_DIST_DIR` names an artifact directory.
Run `make build && make test-metadata` for the full Python range/classifier check,
or `make test-metadata DIST_DIR=/path/to/artifacts` to check an existing build.
Missing artifacts or multiple wheels/sdists fail that command, rather than skip.
To include those checks in the full suite, run `METADATA_DIST_DIR="$PWD/dist" make test`
after `make build`.

CI runs `make test-metadata METADATA_PYTHON=python` in **Build compatibility
artifacts**, before uploading `compatibility-dist`; that job gates `ci-required`.
The unit/compatibility suites no longer build distributions. The installed-wheel
CI lane reads `compatibility-dist`, and the unchanged release wheel-tests lane
reads the downloaded `dist/` relative to the checkout (not its scratch directory).
The Azure Functions 2.x cells also install the shared compatibility wheel instead
of building their own. Publishing, release gates and coverage requirements are unchanged.

Before opening a PR, run `ruff format --check <changed-python-files>` (or
`ruff format <changed-python-files>` to fix them). `make format-check` checks
`src` and `tests` without changing files. CI checks those same trees and also
reports formatting failures on changed Python files in a dedicated step.

In **Test and Coverage** CI, the blocking coverage gate is pytest's
`fail_under = 95` on the Python 3.11 matrix cell. Uploading its report to
Codecov is **non-blocking but visible**: an upload failure emits a warning
annotation and an actionable job summary, without relaxing the pytest gate.
Check the upload log for authentication or service/network errors; the owner
maintains the `CODECOV_TOKEN` repository secret from the Codecov dashboard in
**Settings > Secrets and variables > Actions**. Fork PRs do not receive repository
secrets, and Dependabot skips the upload and its failure-reporting step.

Integration tests (Azurite, Cosmos) require docker-compose services. The
Cosmos emulator is known-flaky on GitHub-hosted runners, so CI does not run
it on push or pull_request: it runs nightly on a schedule and on manual
`workflow_dispatch`. Contributors touching Cosmos code should run
`make test-cosmos` locally before merging.

Setup is explicit: install Hatch on PATH (or run `make bootstrap` once and pass
`HATCH=.venv/bin/hatch`), then `make install`. Routine commands never upgrade pip
or reinstall Hatch. `make doctor` only inspects the existing Hatch environment;
it installs nothing and fails when prerequisites for `check` or `test` are missing.
`make check` runs style checks and one type-check pass.

`make test-table` and `make test-cosmos` create a per-run Compose project on
loopback-only default ports, and remove only that project on return, failure or
interrupt. Stop your own conflicting services yourself; these targets never
reuse them. Both targets use `tools/check_junit.py`, the same validator as CI,
to reject empty, skipped, failed or inconsistent reports. The nightly Cosmos
lane remains non-gating and is not release certification.

`make host-smoke` needs Core Tools (`func`), `azurite` and the development extras.
It starts native and true-streaming hosts on run-selected ports, exercises native
invoke plus incremental streaming/error/disconnect cases, rejects startup/indexing
log failures, and reaps its own process groups and temporary storage on success,
failure or interrupt. It does not stop existing hosts or emulators.

`make host-lanes` needs the same tools and runs in the `table` Hatch environment
(the Blob and Table extras). It starts two lock-test hosts, a Platform SDK host and
topology-refusal hosts over one Azurite, runs `tests/e2e/test_langgraph_lock_e2e.py` and
`tests/host_lanes/test_platform_sdk.py` through `tools/check_junit.py` so that a skip
fails the run, and reaps its own process groups and temporary storage the same way. Pass
`--lane locks|platform|topology` to the runner (`python -m tools.run_host_lanes`) to run
one lane. The result is local evidence on an emulator, not Azure certification.

`make e2e-azure` needs `E2E_BASE_URL`, `E2E_BASE_URL_B` and `E2E_STREAM_BASE_URL`
pointing at deployed native, second lock-test and streaming apps. Configure keys
with `E2E_CODE` and `E2E_STREAM_CODE`, never URL query parameters. Missing targets,
unexpected skips and zero selected tests fail rather than certify a deployment.
The release workflow uses the same strict validator; its certification and gates
are unchanged.

`COSMOS_DISABLE_SSL=true|1|yes` makes `create_cosmos_checkpointer` skip TLS
certificate verification for the emulator's self-signed certificate. It is
honored only when the endpoint host is `localhost`, `127.0.0.1` or `::1`; for
any other host it is ignored, verification stays on and a warning is logged.

`create_cosmos_checkpointer` also validates the endpoint before any Cosmos
client is built and raises `ValueError` for an ambiguous endpoint (backslash,
whitespace/control or non-ASCII character, userinfo such as `user@host`, or a
host that `urlparse`, `urlsplit` and `urllib3` do not extract identically),
whether or not `COSMOS_DISABLE_SSL` is set. azure-cosmos itself skips
verification for hosts it parses as `localhost`/`127.0.0.1`, so such an
endpoint must never reach the SDK.

Compatibility note: non-loopback emulator hostnames (for example a Docker
network name such as `https://cosmos-emulator:8081`) cannot use
`COSMOS_DISABLE_SSL`. Use a trusted certificate (`REQUESTS_CA_BUNDLE`;
`SSL_CERT_FILE` is not honored by this client) or a supported loopback endpoint published on the host.

## GitHub Actions Pinning

All external `uses:` references in `.github/workflows/` MUST pin to a
full 40-character commit SHA with a trailing comment documenting the
released version or channel. Example:

```yaml
- uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6
```

This applies to first-party (`actions/*`, `github/*`, `azure/*`) and
third-party Actions alike.

**Rationale.** Mutable tags (including immutable-looking version tags
like `@v6.0.1`) can be retroactively moved by an attacker who gains
write access to the upstream repository. The
[`tj-actions/changed-files` compromise (CVE-2025-30066, March 2025)](https://github.com/advisories/GHSA-mrrh-fwg8-r2c3)
demonstrated this exact failure mode: ~23,000 repositories had their CI
secrets exfiltrated, and only workflows that pinned to a commit SHA were
safe. GitHub's own guidance now identifies SHA pinning as
[the only way to use an Action as an immutable release](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#using-third-party-actions),
and OpenSSF Scorecard's
[`Pinned-Dependencies` check](https://github.com/ossf/scorecard/blob/main/docs/checks.md#pinned-dependencies)
flags anything weaker as Medium-risk.

Dependabot updates SHA-pinned references on the configured schedule and
keeps the trailing version comment in sync, so the human-readable
context never drifts from the pinned commit.

**Approved exceptions.** The following mutable refs are the only ones
permitted in this repository and are flagged with an inline comment at
the call site:

- `pypa/gh-action-pypi-publish@release/v1` — PyPA-maintained stable
  release channel; this pinning style is explicitly recommended upstream.
- Local composite actions (`uses: ./...`) — versioned with the repo.

When adding a new external Action, resolve the SHA with
`git ls-remote <repo-url> 'refs/tags/<tag>^{}'`. The trailing `^{}`
**dereferences** the ref to its target commit; without it, an
*annotated* tag returns the tag-object SHA instead of the commit SHA,
and the tag-object SHA is **not** a valid `uses:` target.

```bash
# Annotated tag — the two forms return *different* SHAs:
git ls-remote https://github.com/Azure/login 'refs/tags/v3.0.0' 'refs/tags/v3.0.0^{}'
# 93381592...   refs/tags/v3.0.0       <- tag object, do NOT pin to this
# 532459ea...   refs/tags/v3.0.0^{}    <- commit, pin to this one

# Lightweight tag — only the non-deref form returns a row, and it is
# already the commit SHA. Always include the `^{}` form anyway so the
# same command works for both tag types:
git ls-remote https://github.com/actions/setup-python 'refs/tags/v6' 'refs/tags/v6^{}'
```

Single-quote the `refs/...^{}` argument so the `{}` and `^` are not
interpreted by your shell (notably zsh with `EXTENDED_GLOB`).

## Example Coverage Policy

For complex checkpoint, authorization, streaming and cancellation regressions,
add a concise English intent note when the name and assertions do not explain
the invariant. Explain coordinated events, injected failures and ownership
boundaries rather than narrating every assertion. Fake backends prove adapter
behavior, not real Azure or cross-instance certification; name that limitation
when it matters. Do not add notes mechanically to simple tests.

Examples are part of the supported API experience and should stay verified.

- Keep one representative example for the minimal LangGraph agent workflow.
- Keep one complex example for multi-agent orchestration and state persistence.
- Add or update smoke tests whenever an example changes.
- Prefer lightweight smoke coverage over infrastructure-heavy end-to-end tests.

## Issue Scope

When opening or editing an issue, follow the
[Issue Conventions in AGENTS.md](AGENTS.md#issue-conventions), including the
repository-scope checks. Describe this repository's problem and acceptance
criteria, and explain why any external repository reference is relevant.

## Title Convention

This section is the single source of truth for issue, pull request, and commit titles in this repository. `AGENTS.md`, the pull request template, the issue forms, and the documentation site point here instead of restating the rules.

Issues, pull requests, and the final squash commit on the default branch all use:

```text
type: description
type(scope): description
```

- Write titles in English.
- `type` and `scope` are lowercase. Put exactly one space after the colon.
- `scope` is optional. Add it only when it narrows the area; do not add one just for uniformity.
- `description` states what changes and why, concisely. Start with a lowercase word, but keep the spelling of API names, acronyms, error codes, and proper nouns.
- No trailing period.
- No priority, size, status, or owner markers, and no bracket prefixes such as `[Bug]`, `[Feature]`, or `[WIP]`. Use labels and Draft pull requests instead.
- An issue title may describe the problem; the pull request title describes the change that resolves it. Keep them aligned when they cover the same work, without forcing identical wording when the scope differs.
- Do not put issue numbers in pull request titles. Link issues in the pull request body with `Closes #123` or `Refs #123`.

| type | use for |
|---|---|
| `feat` | a user-facing feature or capability |
| `fix` | a bug fix in product behavior |
| `docs` | documentation only |
| `test` | adding or changing tests |
| `perf` | a performance improvement |
| `refactor` | internal restructuring that keeps external behavior |
| `ci` | CI and automation workflows |
| `build` | build, packaging, and dependency changes |
| `chore` | maintenance that fits none of the above |
| `style` | formatting only, no behavior change |
| `revert` | reverting an earlier change |

A breaking change adds `!` before the colon: `type!: description` or `type(scope)!: description`. Whether a change is breaking, and how it is released, is decided by the Release Process in [AGENTS.md](AGENTS.md), not by the title alone. Release Please reads these titles from the squash commits on `main`: `fix` and `feat` drive the version bump, and the other types only shape the changelog.

Examples:

```text
fix: release the thread lock after a graph error
feat(streaming): add an optional event limit
docs(readme): clarify the local setup steps
refactor!: rename the public configuration keys
```

Not accepted: `[Bug] crash on save` (bracket prefix, no type), `Fix: crash on save` (uppercase type), `feature: add export` (type not in the table), `fix: crash on save.` (trailing period), `fix: release the thread lock after a graph error (#503)` (the pull request number belongs in the body).

Pull requests are squash-merged and the pull request title becomes the final commit title. GitHub appends the pull request number by itself, so `fix: release the thread lock after a graph error` lands on `main` as `fix: release the thread lock after a graph error (#503)`. Do not write that number into the title yourself — the check rejects a title ending in `(#123)`, because it would be doubled. Link the issue from the pull request body instead, for example `Closes #503`. The **PR title** check validates the format whenever a pull request is opened, edited, reopened, or updated. Individual commits on a branch are not checked.

Issue titles are not enforced. Anyone can open an issue without knowing this convention; maintainers adjust the title during triage.

### Breaking-change classification

Use `!` in a Conventional Commit/PR title, or a `BREAKING CHANGE:` footer in the **final squash commit**, only when a change makes an existing supported consumer usage incompatible or changes a supported observable contract incompatibly. Base the decision on verified behavior before and after the change, not on size, risk, security motivation, or which tool authored it. Public APIs, required configuration, authentication defaults, supported runtimes, published output formats, and generated application behavior may all be contracts. A security fix can be breaking if it changes a supported calling pattern; a refactor, dependency bump, CI scan, test change, or performance fix is not automatically breaking. When unsure, verify the affected contract before applying the marker. The existing PR-title check validates **syntax**, not compatibility. A squash commit marker influences Release Please; a Markdown `## BREAKING CHANGE` heading in a PR body alone does **not** guarantee that the final commit is marked breaking.

The API and Native HTTP contract gate (`tools/api_contract.py`, see
[API contract gate](docs/api-contract-gate.md)) fails a pull request that
breaks the Stable or Beta public API or the Native HTTP contract unless
`tests/contracts/classifications.json` records a traceable classification.

#### Local classification examples

- **Breaking:** changing a documented graph invocation option or authentication
  requirement for a previously supported caller (hypothetical; verify the actual
  contract in the proposed PR). Restricting formerly accepted native client
  configuration in #740 was marked breaking and includes migration guidance.
- **Non-breaking:** internal run-lock cleanup that preserves documented calls,
  errors and outputs. Compatible security hardening is not automatically breaking.
- **Generated output:** assess callers that consume regenerated output separately
  from existing deployments. Verify an uncertain contract before classifying it.

## Releases

Merging to `main` does not publish a release.

Release Please keeps an open **Release PR** showing what the next version would
be; merging that PR is what cuts the release, and it is what creates the `v*`
tag that starts the publish workflow. Publishing happens only after the release
verification requirements have been satisfied. Contributors never need to bump a
version or tag anything in an ordinary pull request.


For this primarily solo-maintained package, routine user-facing changes
are normally bundled into **at most one planned release per week**, with
no obligation to release during a quiet week. Urgent security, production
regression and failed-candidate corrections can be shipped sooner after
the maintainer records the reason in the Release PR.

See [release cadence and emergency exceptions](docs/release_process.md#release-cadence)
and `AGENTS.md` for the maintainer-only procedure.

## Code of Conduct

Be respectful and inclusive. See our [Code of Conduct](CODE_OF_CONDUCT.md) for details.
