# AGENTS.md

## Purpose
`azure-functions-langgraph` deploys LangGraph compiled graphs as Azure Functions HTTP endpoints with zero boilerplate.

## Read First
- `README.md`
- `CONTRIBUTING.md`

## Working Rules

### Test Coverage
- Maintain test coverage at **95% or above** for committed changes and PRs.
- Run `hatch run pytest --cov --cov-report=term-missing -q` to verify before submitting changes.
- Any PR that drops coverage below 95% must include additional tests to compensate.
- Runtime code must remain compatible with Python 3.10+.
- Public APIs must be fully typed.
- Graph registration must remain protocol-based — accept any object satisfying `LangGraphLike`, not just `CompiledStateGraph`.
- Keep documentation examples, app behaviour, and tests synchronized.
- Bumping version is automatic — `make release-patch` updates `__version__`, and the public-API test reads it back via `importlib.metadata.version(...)` so no test edits are needed.

### Documentation & Translations
- When a change touches `README.md` or any English documentation, update the translated READMEs (`README.ko.md`, `README.ja.md`, `README.zh-CN.md`) **in the same PR** so translations never drift from the English source.
- This applies to any code change that alters documented behavior, CLI output, or the ecosystem/package table — not just direct edits to prose.
- If a full translation cannot land in the same PR, add a short "translation pending" note to the affected translated file and open a tracking issue before merging.

## Issue Conventions

Follow these conventions when opening issues so the backlog stays consistent with sibling DX Toolkit repositories.

### Title

- Use Conventional Commit prefixes: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `build:`, `perf:`.
- Add a scope qualifier when it narrows the area: `feat(graph):`, `docs(adapter):`, `refactor(runtime):`.
- Keep the title imperative, under ~80 characters, no trailing period.
- Do **not** put `[P0]` / `[P1]` / `[P2]` (or any priority marker) in the title — priority is tracked with a `priority:p0` / `priority:p1` / `priority:p2` label.

### Body

Use the following sections, in order, omitting any that do not apply:

```
## Context
What problem this issue addresses and why now. Note the target release (e.g. vX.Y.Z) here if known.

## Acceptance Checklist
- [ ] Concrete, verifiable items.

## Out of scope
- Items intentionally excluded, with links to the issues that track them.

## References
- PRs, ADRs, sibling issues, external docs.
```

### Labels

- Apply at least one of `bug`, `enhancement`, `documentation`, `chore`.
- Apply exactly one `priority:p0` / `priority:p1` / `priority:p2` label to record priority (replaces the old `## Priority` body line).
- Add `area:*` labels when they exist in the repository.
- Use `blocker` only when the issue blocks a release.

### Umbrella issues

When splitting a large piece of work into focused issues, keep the umbrella open as a tracker that links each child issue with a checkbox; close it once every child is closed or explicitly deferred.

## Validation
- `make test`
- `make lint`
- `make typecheck`
- `make build`

## Release Process
- Version is managed via `hatch` (dynamic from `src/azure_functions_langgraph/__init__.py`).
- **Do NOT manually edit version strings.** Use the Makefile targets below. The public-API test reads `__version__` against `importlib.metadata.version(...)`, so no test changes are needed when bumping.

### Commands
- `make release-patch` — bump patch version, update changelog, tag, and push
- `make release-minor` — bump minor version, update changelog, tag, and push
- `make release-major` — bump major version, update changelog, tag, and push
- `make release VERSION=x.y.z` — set explicit version, update changelog, tag, and push
- `make tag-release VERSION=x.y.z` — create and push an annotated tag (used internally by release targets)

### Tiered runtime verification (what gates a release)

Release verification is layered; each tier is a **pre-publish gate**, not a post-publish check. No version reaches PyPI until every tier passes:

| Tier | Runs where | Catches |
| --- | --- | --- |
| `lib-tests` | publish-pypi.yml (per publish) | library unit regressions (against the built wheel) |
| `verify-azure-certification` | publish-pypi.yml (per publish) | requires a fresh, SHA+version-matched **real-Azure** certification for the exact release commit |
| Azure Release Certification (`e2e-azure.yml`) | `workflow_dispatch`, per release | cloud-only drift — deploys this package's own `examples/e2e_app/` to real Azure (Y1 Consumption), runs the native LangGraph HTTP e2e (health/invoke/stream), and records a certification artifact keyed by commit SHA + version. **Certified per release, not per publish.** |

Unlike the sibling repos, this package has **no** cookbook host-smoke tier. The real-Azure e2e deploys this package's own example app and exercises the native LangGraph routes, so it *is* the package-native runtime proof. Critically, the certification builds the candidate **wheel from the release ref** and bundles it into the example app (`examples/e2e_app/wheels/`), so Azure certifies the release commit's source — not the last-published PyPI build.

### Flow
1. `make release-patch` (or `-minor` / `-major`) on `main`
2. This runs: `hatch version` → `git commit` → `make changelog` → `git commit` → `git tag` → `git push`
3. **Real-Azure certification (required once per release, before the final publish).** Before (or immediately after) pushing the release tag, dispatch the **Azure Release Certification** workflow on the exact release commit and version:
   - `gh workflow run e2e-azure.yml --ref main -f ref=<release-sha> -f version=<x.y.z>`
   - The run deploys `examples/e2e_app/` (with a wheel built from `<release-sha>`) to real Azure, executes the live e2e suite (health/invoke/stream), and uploads the `azure-cert` artifact (keyed by commit SHA + version).
4. Tag push triggers the **Publish to PyPI** workflow. The `publish` job runs only after `build → lib-tests → verify-azure-certification` all pass, and it uploads the exact artifact that was built (it never rebuilds). `verify-azure-certification` requires a successful, SHA+version-matched, non-stale (<14 day) certification for the release commit; without it the publish gate fails and the version stays unpublished.
5. Update `docs/changelog.md` separately if needed (different format from `CHANGELOG.md`).
6. **Failed-gate recovery (stuck tag).** A git tag is immutable and may already have been consumed, so if the gate fails do **not** move or reuse the tag. Fix forward on `main` and cut the next patch tag (`make release-patch`). The unpublished version number is simply skipped.

## Branch Hygiene

- Merged PR branches are deleted automatically ("Automatically delete head branches" is enabled on this repository); keep that setting on.
- When merging from the CLI, always pass `--delete-branch` (e.g. `gh pr merge --squash --delete-branch`) so the head branch is removed.
- Never delete `main` or `gh-pages`, and never delete a branch that still has an open PR.
- Run `git fetch -p` periodically to prune stale local tracking refs.
