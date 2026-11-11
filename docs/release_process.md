# Release Process

## Optional downstream notification credential

The post-publish notification uses `COOKBOOK_DISPATCH_TOKEN` only in its
dispatch step, with a fixed HTTPS destination and bounded connection/total
timeouts. Its job has only `contents: read`; do not broaden `GITHUB_TOKEN`
permissions to enable cross-repository dispatch. The secret must be a
fine-grained token limited to the destination repository's `contents: write`,
or an equivalently scoped GitHub App installation token.

Source code cannot inspect a stored secret's actual scopes or prove rotation.
The repository owner must audit the credential in GitHub settings, replace it
with the scoped credential, verify the first intended notification, and revoke
the old credential. Record only token identity/expiry and cutover/revocation
confirmation, never the token value. Missing notification credentials still
skip this optional post-publish step; no publication or required CI gate is
relaxed. Issue #382 remains open until owner-operated credential evidence exists.

This document describes how a new version of **azure-functions-langgraph** reaches PyPI.

Releases are automated by [Release Please](https://github.com/googleapis/release-please). It decides the
version, writes the changelog, and cuts the tag. It does **not** publish — publication is a separate,
gated step.

---

## Who owns what

| Component | Owns |
|---|---|
| `release-please.yml` | version calculation, `CHANGELOG.md`, the Release PR, the tag, the GitHub Release |
| `release-please.yml` → `standardize-release-notes` | presentation of the GitHub Release body only (layout + credits, see [Release notes layout](#release-notes-layout-and-credits)) |
| `e2e-azure.yml` | real-Azure certification of one exact commit |
| `publish-pypi.yml` | the only path that uploads to PyPI (runs on the release tag push, or via `workflow_dispatch` to retry) |

**Do not hand-edit** `src/azure_functions_langgraph/__init__.py` version strings, `CHANGELOG.md`, or
`.release-please-manifest.json`. Release Please maintains all three.

---

## Step 1: Merge Conventional Commits

Version bumps are derived from commit messages on `main`:

| Commit | Bump |
|---|---|
| `fix:` | patch (`0.8.2` → `0.8.3`) |
| `feat:` | minor (`0.8.2` → `0.9.0`) |
| `feat!:` / `fix!:` / `BREAKING CHANGE:` footer | minor while pre-1.0 (see below) |

While this package is pre-1.0, `bump-minor-pre-major` is enabled, so a breaking change moves to the
next minor (`0.9.0`) rather than jumping to `1.0.0`. Going to 1.0 is a deliberate, separate decision.

Use scopes for more context: `fix(scope): short imperative summary`

### Changelog categories

| Prefix | Changelog section |
|--------|--------------------|
| `feat:` | Features |
| `fix:` | Bug Fixes |
| `perf:` | Performance |
| `revert:` | Reverts |

`docs:`, `refactor:`, `style:`, `test:`, `ci:`, `chore:`, and `build:` commits are accepted but
marked `"hidden": true` in `release-please-config.json`, so they do not appear in `CHANGELOG.md` and
do not by themselves justify a release — a batch containing only these types produces no Release PR.

These are configured in `release-please-config.json` under `changelog-sections`.

---

## Step 2: Merge the Release PR

Release Please keeps an open **Release PR** titled like `chore(main): release 0.9.0`. It contains the
version bump and the changelog entry.

Merging that PR is the act of cutting a release. On merge, Release Please tags the release commit
(`v0.9.0`) and publishes the GitHub Release. That GitHub Release is created before the gates of step 3
run and is not removed when one of them fails, so it is not evidence that the version is on PyPI; see
[Release status](release-status.md) for the tagged versions that have no files on PyPI.

Release Please runs with `RELEASE_PLEASE_TOKEN` (a fine-grained PAT) rather than the default
`GITHUB_TOKEN`, so the required status checks run on the Release PR and it is merged under the same
branch-protection rules as any other PR. The PAT expires; regenerate it and update the secret before
it does, or no Release PR will appear.

---

## Release cadence

**Default:** the maintainer batches routine user-facing changes into **at
most one planned release per week**, and only when the package is ready.
A quiet week requires no release. Ordinary development PRs continue to
merge when approved; Release Please keeps the open Release PR updated.
Merging that Release PR is a separate, explicit maintainer decision.

**Out-of-cycle:** orchestration corruption or stuck runs, security/lease hazards affecting production, or failed tagged candidate may justify
an earlier patch. Before that merge, briefly document the affected users
and why it cannot wait. Do not add an automated waiting period or force
weekly releases just to satisfy a schedule.

Before a routine release, verify the newest **actual PyPI distribution**
separately from GitHub Release status, review the user-facing changes,
compatibility and migration notes, pending near-term fixes, and readiness
of all relevant package/consumer/Azure verification gates.
This repository additionally reviews durable orchestration state/cancellation, checkpoint fencing limitations and long-running workflow compatibility.
Check that the latest `Release audit` run on `main` is green (or its only findings are
registered exceptions), so a previous release is not left unpublished before the next one is cut.
Preserve Release Please ownership of version/tags/CHANGELOG and keep all
existing tag-specific verification and publication gates intact.

---

## Step 3: The release workflow runs

The tag starts `publish-pypi.yml`. Everything else is automatic:

```
build -> lib-tests, wheel-tests, runtime-audit, azure-e2e -> publish -> notify-cookbook
```

| Tier | Catches |
|---|---|
| `build` | tag/`__version__` mismatch; produces the one artifact that is later uploaded |
| `lib-tests` | library unit regressions against the **source tree** — this is where the ≥95% coverage gate is enforced |
| `wheel-tests` | packaging regressions — runs the **same unit suite (including the `examples/` smoke tests) against the *installed built wheel*** (imports resolve from site-packages, not `src/`), so missing wheel files, bad metadata, or install-only import errors fail fast. This is how the examples are exercised against a built wheel, not just the source tree. Coverage is not re-measured here. |
| `runtime-audit` | candidate runtime dependency audit against the built distribution — resolves and records the dependency closure actually shipped in the artifact |
| `azure-e2e` | cloud-only drift — deploys this package's own `examples/e2e_app/` to real Azure (Y1 Consumption), runs the native LangGraph HTTP e2e (health/invoke/stream), and uploads an `azure-cert` record keyed by commit SHA + version. |
| `publish` | uploads the exact artifact `build` produced; it never rebuilds |

`lib-tests`, `wheel-tests`, `runtime-audit`, and `azure-e2e` all depend only on `build` and run in
parallel; `publish` waits on all four.

`azure-e2e` calls `e2e-azure.yml` as a reusable workflow at the same ref being published, so
certification covers the exact published commit by construction. There is no separate certification
step to dispatch and no freshness window to expire.

Publication uses PyPI Trusted Publishing (OIDC); there is no API token to manage.

To re-run after a failed gate (nothing was uploaded, so the version is still free):

```bash
gh workflow run publish-pypi.yml --ref v0.9.0
```

The dispatch declares no inputs. Selecting the existing tag with `--ref` makes `github.ref_name`
match the package version validation the workflow performs. This retries the *same* release
candidate after a transient or external failure (e.g. a flaky Azure deploy, a PyPI outage). A source
or workflow fix instead requires a new commit on `main` and a new Release PR/tag — it is never
applied retroactively to an already-cut tag. Never move or reuse a tag.

---


## Release notes layout and credits

After Release Please creates the GitHub Release, the `standardize-release-notes` job rewrites
**only the Release body** with `tools/release_notes.py`. It does not create or move tags, does not
edit `CHANGELOG.md`, and runs separately from `publish-pypi.yml`, so it cannot block a release.
For new Release Please bodies, the items are the ones Release Please already generated.
Historical bodies instead preserve their existing items; a compare-link-only body is reconstructed
from compared commits and explicitly labelled as reconstructed. Such historical summaries need not
match `CHANGELOG.md`. A reconstructed commit keeps its `BREAKING CHANGE:` or `BREAKING-CHANGE:`
footer and the lines after it, including paragraphs that follow a blank line, up to the next
trailer: a line that begins with a token without spaces followed by `: ` or ` #`, such as
`Refs: #12`, `Closes #12` or `Signed-off-by: ...`. Prose such as `Migration steps: ...` has a token
with spaces, so it stays. Nothing else in the commit message is published.
The tool refuses to write if an exact local PR/issue number or seven-character commit reference
would be dropped (except unverified references in the replaced New Contributors block), or if a
configured protected phrase would be lost. PRs and all commits share one trailing group, for example
``(#12, #13, `abc1234`)``. Legacy repository references are accepted. Within an item, a Release
Please style ``([#12](URL))`` wrapper, a ``(#12)`` group and a trailing GitHub-generated `in URL`
are read as references. Any other inline Markdown link, `[label](destination "title")`, is kept as
written (runs of whitespace in an item collapse to one space). A link is read the way CommonMark
reads it: its label may nest brackets and contain code spans, its destination is `<...>` or plain
with up to 32 nested pairs of parentheses, its title is quoted with `"`, `'` or parentheses, and a
backslash escapes ASCII punctuation (before anything else it is an ordinary character). A link
inside another link's label is a link of its own and the brackets around it are text. An image may
sit inside a link, and a link inside a valid image's description leaves the brackets outside the
image alone. The label is never read as a reference, and a link to another repository is never
treated as local attribution evidence. A foreign link labelled `#12` is therefore neither parsed as
local PR 12 nor credited to that PR's author, and it does not satisfy the check for a lost local
`#12`. The check replaces each link with a space, so `#` before a link and `12` after it never join
into `#12`. Only inline links on a single line are recognized: reference-style links
(`[label][ref]`), HTML tags and autolinks (`<https://...>`) and links that span lines are not, so a
bracket inside an HTML tag or autolink counts like any other character.

Layout (empty sections are omitted):

1. `## vX.Y.Z (YYYY-MM-DD)`
2. Notes: status annotations such as "not published to PyPI"
3. Highlights
4. Breaking Changes, followed by the upgrade steps
5. Features, Bug Fixes, Performance, Reverts
6. New Contributors
7. Documentation, maintenance and other changes: collapsed when mixed with classified user-facing
   items, or visible under `### Changes` when all items are unclassified. Release Please normally
   hides maintenance types, but historical bodies can contain them.
8. `**Full Changelog**` compare link

Credit rules:

- This is a primarily solo-maintained project: omit repetitive `by @yeongseon` on owner-authored changes, while retaining PR/issue references.
- Credit API-verified external human contributors with `by @username`, even when their PR was squash-merged by the owner. Authorship evidence comes only from API commit authors and merged PR authors. Co-author trailers, including GitHub noreply addresses, are claimed identities and are not used as evidence.
- Exclude bots and configured non-human accounts from human credits.
- Show **New Contributors** only when prior-commit lookup and a complete, well-formed merged-PR search verify no prior contribution from an external human; uncertain/API-error results omit the claim. Omit the section for owner-only releases.

Compare-only reconstructed commit subjects have HTML tags removed, Markdown brackets escaped and
unsolicited `@mentions` rendered as code spans. Existing Notes, Highlights and Upgrade blocks keep
their lines in order, except that HTML comments, thematic breaks (`---`, `***`, `___`), lines
starting with `<details` or `</details` and `**Full Changelog**:` lines (the layout regenerates
those) and trailing whitespace are dropped, runs of blank lines collapse to one, blank lines at the
start and end of a block are trimmed, and links to a configured legacy repository are rewritten to
the current one. A `<details` or `</details` line also ends the block: what follows is read as
ordinary release text, so its bullets become items of the layout and its other lines join Notes.
Generated `by @username` and New Contributors credits are deliberate mentions;
notification delivery on release edits has not been verified.

Release-specific statements that must never be lost (for example a release that was not
published) are listed under `protected` in `.github/release-notes.json`; the tool fails instead of
dropping them.

Maintainer clarifications use `annotations` in the same JSON configuration, keyed by tag and block:

```json
{"annotations": {"vX.Y.Z": {"notes": ["Status explanation", "Supporting evidence"]}}}
```

Supported blocks are `notes`, `highlights` and `upgrade`, each containing an array of lines. An
annotation is already present only when its entire exact line sequence occurs in its target block;
a shared first line is not enough. To change an existing annotation, remove its old lines from the
release body, update the configuration, then re-render with `--force`. Otherwise the new block is
appended, not substituted for an old partial block. Review the read-only render before any apply.

`apply` normally skips a body carrying the standardization marker. `--force` bypasses only that
shortcut, never reference/phrase verification. `apply --dry-run` prints the candidate for an
unstandardized body; for an already standardized body it reports that it would be skipped and exits
zero. Use `render` for an unconditional read-only preview (including standardized releases).

`--backup DIRECTORY` exclusively creates a per-tag JSON snapshot before editing; an existing file
causes an error and no release write. It contains `tagName`, the original `body`, `publishedAt` and
the release `id`; `preview --backup DIRECTORY --out DIRECTORY` keeps that publication date in the
heading. Preserve the first snapshot as the rollback source; select a new directory for a later
intentional edit instead of overwriting it. If the PATCH itself fails, the snapshot stays on disk
and still holds the original body, so retrying with the same directory stops with `File exists`:
check the release, then remove that file or choose a new directory. The final API read compares the
current body with the build snapshot and aborts on a concurrent change. This reduces races but is
**not atomic**: a change between that read and the PATCH is still possible.

Inputs and generated bodies are refused above 500,000 characters total or 20,000 characters on one
line (GitHub's release-body limit is 125,000). The patterns that read a body are written not to
backtrack on adversarial lines, and tests run worst-case shapes at these limits. API work
shares a 120-second budget that is checked before and after each request rather than enforced by
interruption (each socket operation waits at most 30 seconds), and each pagination loop stops at 50
pages of 100 records; exceeding a limit fails closed rather than truncating. The workflow has a
ten-minute timeout, which is the hard outer bound, retains available backups as an artifact and
warns in its summary if standardization fails. Its `github.token` has `contents: write`, a broader
permission than release-body editing: the tool edits only `body` by implementation, not token-level
enforcement.

```bash
# Needs a token: export GH_TOKEN=$(gh auth token)
# Unconditional read-only preview
python3 -m tools.release_notes render v0.13.0
# Dry-run apply (reports a skip when already standardized)
python3 -m tools.release_notes apply v0.13.0 --dry-run
# Re-render an already standardized release (it is skipped otherwise)
python3 -m tools.release_notes apply v0.13.0 --force --backup ./release-backup
```

Editing a historical Release never republishes a package and never touches the tag.

---

## What is no longer used

| Retired | Replacement |
|---|---|
| `make release-patch` / `release-minor` / `release-major` / `release` | merge the Release PR |
| `make changelog` / `make commit-changelog` (git-cliff) | Release Please writes `CHANGELOG.md` |
| `make tag-release` | Release Please creates the tag |
| `make publish-pypi` (local `hatch publish`) | the `publish` job in `publish-pypi.yml` |
| `cliff.toml` | `release-please-config.json` |
| manual Azure certification dispatch | the `azure-e2e` job in `publish-pypi.yml` |

These Makefile targets are **deleted**, not merely discouraged, so nothing can create a second writer
for the version, changelog, or tags — or push an ungated artifact to PyPI.

`make publish-test` (TestPyPI) is unaffected.

---

## Recovery

| Situation | Action |
|---|---|
| Any gate failed | Nothing was uploaded. Fix the cause and re-run `publish-pypi.yml` on the same tag, or fix forward on `main` and let the next Release PR cut a new version. Never move or reuse a tag. |
| Tag exists but was never published | A valid resting state. Re-run publish, or abandon the version and let the next release take the following number. The tagged versions in this state, with what the retained records show about each, are listed in [Release status](release-status.md). |
| Release PR stopped appearing | Check for a stale `autorelease: pending` label on an already-merged Release PR. Release Please treats that as a release in flight and will not open another. This failure is silent — the workflow still reports success. |
| Automation unavailable (break-glass) | Bump `__version__`, match `.release-please-manifest.json`, commit, tag, push. The tag starts the same gated workflow — never bypass it. |

---

## Still-current Makefile commands

| Task | Command |
|------|---------|
| Build distributions | `make build` |
| Publish to TestPyPI | `make publish-test` |
| Show current version | `make version` |

To test a local build:

```bash
pip install dist/azure_functions_langgraph-<version>-py3-none-any.whl
```

---

## Release audit

`make audit-releases` runs the stdlib-only, read-only audit of stable numeric tags,
GitHub Releases, tag source versions and ancestry, tag-specific `publish-pypi.yml`
runs/jobs, and PyPI files/provenance. A GitHub Release is not a PyPI distribution.
Only observed `vX.Y.Z` tags/releases and `X.Y.Z` PyPI versions are audited; other
formats are informational notes, and gaps in version numbering are not defects.
Numeric draft releases are ignored with a note, not used as publication evidence.
A numeric release marked prerelease, or a published release without a recorded tag,
is a defect even while its workflow is in flight.
The audit also runs, read-only, in the `Release audit` workflow
(`.github/workflows/release-audit.yml`); see [Automated runs](#automated-runs).

```bash
make audit-releases AUDIT_ARGS='--snapshot-out /tmp/release-facts.json --format markdown'
make audit-releases AUDIT_ARGS='--snapshot-in /tmp/release-facts.json --now 2026-11-10T00:00:00Z --format json'
```

Set `AUDIT_PYTHON=.venv/bin/python` to use an existing environment instead of the
default Hatch interpreter. `AUDIT_ARGS='--help'` needs no network. Live collection
uses only HTTPS GET to `api.github.com` and `pypi.org`, never follows redirects,
and has per-request and overall deadlines (`--timeout`, `--deadline`, in seconds).
Optional `GH_TOKEN` or `GITHUB_TOKEN` credentials are sent only to GitHub; snapshots
contain facts, not credentials. API failures, malformed evidence/registry and
pagination beyond 50 pages of 100 fail closed. Offline replay makes no requests;
`--now` fixes the evaluation clock, while `collected_at` records collection time.
Table (default), Markdown and stable-key JSON output retain evidence links.
Text boundaries reject terminal controls (including DEL/C1) and Unicode line/paragraph
separators. Markdown escapes external syntax and creates links only for HTTPS URLs
without whitespace, closing parentheses or angle brackets; table output stays plain.

`PUBLISHED` means these recorded checks are consistent, not that the audit built,
certified or uploaded anything. `PENDING` is non-failing only when uncovered findings
are waiting for a run or PyPI files, while the run is unfinished or no run exists
within the six-hour grace period measured from tag commit time or release publication.
Observed job failures/cancellations, head-SHA mismatches, source-version and ancestry
defects are never deferred. Other discrepancies are `DEFECT`.
Exit status is 0 without defects (including `PENDING` and `EXCEPTED`), 1 with any
defect, and 2 for collection, usage or registry errors.

`.github/release-audit-exceptions.json` is the explicit historical exception
registry, paired with the [release status table](release-status.md). Adding an
exception is a maintainer decision requiring retained run evidence and a newer
published successor, not a way to waive new failures. The registry defines full,
non-cumulative gate eras, the exact publish job, required jobs, required job prefixes,
optional prefix minimum counts and the provenance threshold. The v0.13.0 era requires
eleven runtime-audit jobs; earlier runtime-audit eras require one. All matching jobs
must succeed; even optional jobs may not fail or be cancelled. Each exception requires
`failed_jobs` and `skipped_jobs` lists: named jobs must exist with those exact conclusions.
Missing or changed jobs produce uncovered evidence mismatches, not message-based waivers. Registered
failures print their covered findings as `EXCEPTED`; new gate failures, changed run
evidence, missing successors or newly published files still fail. The uncertain
v0.5.3 cause remains explicitly unconfirmed. Publisher repository aliases account
for the repository rename, not arbitrary publisher identities.

Every tag-specific workflow run is recorded with its event, ordered by id. Publication
evidence is the latest run whose era-specific publish job succeeded, or the latest run
when none succeeded. Later runs remain informational notes for the version rather
than replacing successful publication evidence.

From `provenance_since`, every PyPI file needs at least one attestation, and every
attestation is checked. The base64 in-toto publish statement must name exactly that
file and its PyPI SHA256. Fulcio certificate claims must bind the repository (or a
registered alias), tag commit, `refs/tags/<tag>` and selected publish run id; publisher
workflow, repository, kind and environment are also checked. Malformed base64, DER,
JSON or semantic evidence is an error (exit 2), not absent provenance. PyPI verified
the signature and transparency log at upload; this audit reads the claims PyPI serves
and binds them to the recorded evidence. It does **not** verify the Sigstore signature
or transparency proof itself. Offline snapshots are replay inputs, not authenticated
certificates of collection.

The audit never edits releases, moves tags, republishes files, or writes version
files or `CHANGELOG.md`. Release Please, GitHub Actions and Hatch retain their
existing authority and gates; recovery still requires a maintainer decision.

### Automated runs

`.github/workflows/release-audit.yml` runs `python -m tools.audit_releases` in three situations:

| Trigger | Purpose |
|---|---|
| `pull_request` touching the audit, its registry, the workflow or `docs/release-status.md` | exercises the change against the real GitHub and PyPI facts; path-filtered, so it is not a required check |
| `workflow_run` after every `Publish to PyPI` run, whatever its conclusion | surfaces a failed or incomplete publication right after the release event (re-checked up to three times, two minutes apart, because PyPI can lag a finished run) |
| daily `schedule` (and `workflow_dispatch`) | catches a tag whose publish run never appeared (a `PENDING` verdict becomes a defect after the six-hour grace period) |

A new defect makes the run **red** (exit 1); an audit that cannot collect or parse
evidence is also red (exit 2) and is not a verdict on any release. The Markdown report
is in the job summary and, with the collected facts, in the `release-audit` artifact. The
workflow reports through its exit status only: it opens no issue and edits no release.
Failed scheduled runs are announced through GitHub's own workflow notifications.

The workflow has `contents: read` and `actions: read` only, uses no secret, never checks
out a ref taken from the triggering run, and cannot publish, tag, or edit a release or
version file. It is not a dependency of `publish-pypi.yml`, so it does not gate
publication: the `build`, `lib-tests`, `wheel-tests`, `runtime-audit` and `azure-e2e`
tiers still decide whether PyPI is uploaded. The audit confirms afterwards that the
outcome matches. The documented release gate itself is not weakened or replaced.

### When the audit is red: fix-forward and metadata correction

1. Read the finding and its evidence links. Do not treat the GitHub Release as proof of
   delivery.
2. If the cause was transient or external, re-run `publish-pypi.yml` on the **unchanged**
   tag (see [Recovery](#recovery)). Never move or reuse a tag, and never upload files
   outside that workflow.
3. Otherwise fix forward: merge a `fix:` change and let Release Please cut the next
   version. Once a later version is published, record the unpublished tag by a pull
   request that adds an entry to `.github/release-audit-exceptions.json` **and** a row to
   [Release status](release-status.md) with the failed run id, its conclusion, the first
   published successor and whether the cause is confirmed. An entry needs retained run
   evidence; an unprovable cause is recorded as unconfirmed, not guessed.
4. Metadata correction: add a short, factual status note at the top of the GitHub Release
   of the unpublished version (a maintainer action; the audit never edits releases). Keep the
   change summary. Suggested text: "Status: this tag's `Publish to PyPI` run failed
   ([run N](URL)), so <version> was not published to PyPI. First published successor:
   <successor>. See [Release status](https://yeongseon.github.io/azure-functions-langgraph-python/release-status/)."
   State only what the records show; do not claim a cause that was not verified, and do not
   describe a version as available unless PyPI lists its files.

User-facing release notes may therefore show only these statuses: *published* (PyPI
lists the files; no note needed), *not published* (the note above, with the first
published successor) and *publication pending* (a run is in flight). They never state
that a GitHub Release is itself a distribution.

## Related

- [CHANGELOG.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CHANGELOG.md)
- [Release status](release-status.md)
- [Development Guide](development.md)
- [Contributing](contributing.md)
- [Release Please](https://github.com/googleapis/release-please)
