# Contributing Guide

We welcome contributions to the `azure-functions-langgraph` project.

## Branch Strategy

Use GitHub Flow and branch from `main`.

Recommended branch prefixes:

- `feat/` for new features
- `fix/` for bug fixes
- `docs/` for documentation-only changes
- `chore/` for tooling and maintenance
- `ci/` for workflow updates

## Development Workflow

1. Create a branch from `main`.
   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/your-feature-name
   ```
2. Write code and tests.
3. Run the local quality gate.
   ```bash
   make check-all
   ```
4. Push and create a pull request.
   ```bash
   git push origin feat/your-feature-name
   ```

## Project Commands

Install Hatch once in an isolated tool environment, then run `make install`.
Routine targets never upgrade pip or reinstall Hatch. Local execution uses
Hatch's Python 3.11; CI uses its selected interpreter directly.
Maintenance targets (`doctor`, `report`, `clean`) reuse an existing interpreter
without creating or synchronizing a Hatch environment. On a fresh checkout,
doctor reports missing development modules and directs you to `make install`.

| Command | Contract |
| --- | --- |
| `make install` | Idempotent Hatch development environment and local hooks |
| `make install-ci` | Install dev/docs/storage extras into CI's selected Python |
| `make doctor` | Read-only prerequisites; nonzero for missing required tools |
| `make format` / `make format-check` | Explicit formatting mutation / read-only check |
| `make lint` / `make typecheck` / `make security` | Ruff + strict mypy / strict mypy / Bandit |
| `make check` | Complete non-mutating style, types once, security and workflow policy |
| `make test` | Offline/unit/mock suite only; coverage >=95% |
| `make integration` | Owned Azurite Table/Blob, Cosmos, deterministic storage/locks, native and streaming hosts |
| `make verify` | Check + offline tests + all local integration + installed-wheel graph smoke + build + strict docs |
| `make e2e-azure` | Explicit existing deployed candidate; no provisioning |
| `make report` | Display saved evidence only, no tests |
| `make clean` | Generated artifacts only; preserve environments/shared resources |

`make check-all` is an exact alias of `make verify`. `clean-all` retains historical
deep cleanup including `.venv` and bytecode. `test-table`, `test-cosmos`,
`test-openapi`, `certify-examples-*`, `host-smoke`, `deterministic`, `wheel-smoke`,
`build`, `docs` and `cov` are focused **PARTIAL** evidence, not full verification.
`e2e-local` runs owned local hosts; it does not certify Azure.
Certification virtual environments live in a run-owned temporary workspace outside
`test-results/` and are removed on exit; only generated textual evidence is retained.

### Services and evidence

`make check`/`make test` require no Docker, emulator, Functions host or Azure.
`doctor` reports `docker` (Cosmos/Compose), `azurite` (integration/hosts), and
`func` (host smoke) as required-for-target prerequisites without installing them.
Integration prefers the Azurite executable with unique ports and temporary storage;
its Docker fallback and Cosmos use unique `lg-verify-<random>` Compose projects.
Occupied explicit ports are refused (INCOMPLETE), never adopted. Cleanup removes
only owned process groups, temporary directories and the exact owned project.

Compose publication always binds `127.0.0.1`. Override `AZURITE_BLOB_PORT`,
`AZURITE_QUEUE_PORT`, `AZURITE_TABLE_PORT`, `COSMOS_PORT`, and
`COSMOS_PORT_10250` through `COSMOS_PORT_10255` explicitly if needed. SDK direct-mode
Cosmos ports may impose emulator constraints. The owned Cosmos certificate is
fetched only from loopback and trusted for that subprocess run. The installed
Azure Cosmos SDK itself bypasses verification for localhost emulator URLs and
emits InsecureRequestWarning; that local SDK behavior is retained, not hidden.
Never disable TLS
certificate verification for production. Cosmos nightly/manual CI remains
non-gating, not release proof. Locally Cosmos is required for FULL_LOCAL: an
unavailable emulator makes verify INCOMPLETE/nonzero; failing tests make it FAIL.

Gitignored `test-results/` contains `test/{junit.xml,coverage.xml,summary.json}`,
`check/{logs/runner.log,summary.json}` (including failed quality commands),
`integration/{junit/,logs/,table/,cosmos/,summary.json}`, separate host/dependency/
wheel lanes, `verify/{summary.json,environment.json,lanes.json}`, and
`azure-e2e/{junit.xml,summary.json,logs/}`. Summaries carry schema version, command,
lane, required flag, scope, selected/passed/failed/skipped counts, original exit
code and artifact paths. Python/SDK/service/runtime versions and source commit
are recorded; connection strings, Cosmos keys, SAS, tokens and function keys are
redacted. Console and Actions Job Summary use the same renderer, and CI uploads
available evidence with `if: always()`.

PASS requires execution. Missing tools/packages/results, zero/all-skipped tests
or unexpected required skips are INCOMPLETE/nonzero; failed cases/processes are
FAIL. The one explicitly unsupported minimal-family release-policy test remains
an expected offline skip; live skips are never allowed. NOT_APPLICABLE is reserved
for explicitly unsupported lanes, not missing prerequisites.

The `minimum` dependency profile permits only the named cases and reasons in
`tools/verification_skips.py`: two version-v2 cases (LangGraph 1.0 lacks that API),
the OpenAPI module and Blob conformance case (extras absent at the dependency
floor), and the SQLite walkthrough/stream case (SQLite is absent at the floor or
its current extra requires checkpoint 4.x, incompatible with LangGraph 1.0's 2.x).
Default-profile lanes permit none of those skips. Zero execution and all-skipped
results remain INCOMPLETE even when every skip is expected.

`tools/wheel-tests.ini` excludes source-only verification/workflow-tooling tests
before collection; the source-tree suite still executes them. The wheel suite
does not add the checkout or `src/` to Python's import path.

Azure requires `E2E_BASE_URL`, `E2E_BASE_URL_B`, `E2E_STREAM_BASE_URL`,
`E2E_STREAM_CODE`, `E2E_EXPECTED_VERSION`, `E2E_CANDIDATE_SHA256`,
`E2E_CANDIDATE_COMMIT` and `E2E_RUNTIME_PROFILE`. These are declared deployment
provenance, not an inferred remote version endpoint (none exists):
`deployed_artifact_identity=INCOMPLETE` remains explicit even when HTTP tests pass.
Existing Azure
OIDC/deployment certification remains separate. The optional `flex-sdk2` profile
excludes two-app/streaming certification explicitly and never replaces the
Consumption SDK 1.x release gate. Local host success never certifies either Azure
production profile.

Before opening a PR, run `ruff format --check <changed-python-files>` (or
`ruff format <changed-python-files>` to fix them). `make format-check` checks
`src`, `tests` and verification tooling without changing files. CI checks those trees and also
reports formatting failures on changed Python files in a dedicated step.

Integration uses run-owned emulator services. The
Cosmos emulator is known-flaky on GitHub-hosted runners, so CI does not run
it on push or pull_request: it runs nightly on a schedule and on manual
`workflow_dispatch`. Contributors touching Cosmos code should run
`make test-cosmos` locally before merging.

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

## Releases

Merging to `main` does not publish a release.

Release Please keeps an open **Release PR** showing what the next version would
be; merging that PR is what cuts the release, and it is what creates the `v*`
tag that starts the publish workflow. Publishing happens only after the release
verification requirements have been satisfied. Contributors never need to bump a
version or tag anything in an ordinary pull request.


See `AGENTS.md` for the maintainer-only release procedure.

## Code of Conduct

Be respectful and inclusive. See our [Code of Conduct](CODE_OF_CONDUCT.md) for details.
