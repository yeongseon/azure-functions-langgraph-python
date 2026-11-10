# Release status

A git tag or a GitHub Release does **not** mean that a version is available on PyPI.
`azure-functions-langgraph` is distributed through
[PyPI](https://pypi.org/project/azure-functions-langgraph/). An installation that uses PyPI as its only
package source can only select a version whose files were published there.

This page lists the tagged versions that have no files on PyPI, what the retained records show about
each one, and which published version to use instead. For every one of them the `Publish to PyPI` run
for the tag failed.

*Last verified: 2026-11-10, against PyPI, the GitHub Releases API, the git tags and the GitHub Actions
run records. It covers the GitHub Releases from v0.2.0 to v0.13.0.*

The read-only [release audit](release_process.md#release-audit), run manually with
`make audit-releases`, compares GitHub and PyPI facts version by version. Its explicit
`.github/release-audit-exceptions.json` registry mirrors the ten historical rows below,
including the unconfirmed v0.5.3 cause; offline tests guard that correspondence.
Covered findings remain visible as `EXCEPTED`, while new discrepancies fail the audit.
Exceptions pin failed and skipped job names/conclusions, not diagnostic message text.
The audit selects the latest successful publish-job run (otherwise the latest run),
retains later runs as notes, ignores numeric drafts with a note and flags numeric
prereleases. It binds every PyPI-served attestation to its file, tagged source and
publish run, but does not independently verify Sigstore signatures or transparency proofs.
No scheduled or CI audit job is added here, and the tool changes no release or publication.

## Why a GitHub Release can exist without a PyPI file

The release process works like this today (see [Release Process](release_process.md)):

1. When the Release PR is merged, Release Please creates the tag and the GitHub Release.
2. The tag starts `publish-pypi.yml`: build, library tests, installed-wheel tests, runtime audit and
   real-Azure certification, then `publish`. Only the `publish` job uploads to PyPI.
3. If a gate fails, `publish` is skipped and no file is uploaded. The GitHub Release stays, because it
   was created before the gates ran and no workflow removes it.

Releases up to v0.8.2 were created by a separate `Create GitHub Release` workflow on the tag push,
which did not wait for the publish run either.

A GitHub Release is therefore the record of a release candidate. Whether that candidate was published is
a separate fact, and PyPI is where to check it.

## Check whether a version is on PyPI

- The [PyPI release history](https://pypi.org/project/azure-functions-langgraph/#history) is the
  authority.
- For scripts, fetch `https://pypi.org/pypi/azure-functions-langgraph/<version>/json` and read its
  `urls` array, which lists the files of that version. The versions below answer `404`. An answer of
  `200` alone does not show that files exist, because PyPI can return a release that has none.
- `pip install azure-functions-langgraph==<version>` for a version without files on the index fails with
  `No matching distribution found`.

The range `azure-functions-langgraph>=0.12.0,<0.13` has published candidates on PyPI (0.12.0 and 0.12.2),
so the missing 0.12.1 does not by itself prevent resolution. An installation still needs a candidate that
is compatible with the Python environment and the other dependency constraints. An exact pin to one of
the versions below has no candidate on PyPI.

## Tagged versions without files on PyPI

| Version | GitHub Release | Publish run | What the retained records show | First published successor |
| --- | --- | --- | --- | --- |
| v0.2.0 | 2026-04-04 | [23981822989](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23981822989) | The trusted-publisher token exchange was refused with `invalid-publisher` (no publisher with matching claims was found). | 0.5.0 |
| v0.3.0 | 2026-04-04 | [23984989252](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23984989252) | The same `invalid-publisher` refusal. | 0.5.0 |
| v0.4.0 | 2026-04-05 | [23993056592](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23993056592) | The same `invalid-publisher` refusal. The publish workflow file is identical at v0.4.0 and v0.5.0, and 0.5.0 was published on 2026-04-06; the retained records do not show what changed outside the repository. | 0.5.0 |
| v0.5.2 | 2026-04-18 | [24605716212](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/24605716212) | Tag gate: `Version in __init__.py (0.5.1) does not match git tag (0.5.2)`. | 0.5.4 |
| v0.5.3 | 2026-04-26 | [24958757167](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/24958757167) | The combined `build-and-publish` job failed. Its only surviving annotation is the attestation notice of the PyPI publish action, and the log has expired, so the failing operation and the error cannot be confirmed. The tagged `pyproject.toml` named the distribution `azure-functions-langgraph-python`, which does not exist on PyPI, and 0.5.4 renamed it back to `azure-functions-langgraph`. That mismatch is the likely cause, not a confirmed one. | 0.5.4 |
| v0.11.0 | 2026-10-11 | [38141259504](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38141259504) | Two gates failed. Installed-wheel tests: `No module named 'scripts'` while collecting `tests/test_check_audit_exceptions.py`. Real-Azure certification: the remote build of the test app failed because it pinned `azure-functions-langgraph<0.11` while the candidate wheel was 0.11.0. #587 followed, and v0.11.1 no longer failed in these two places. | 0.11.4 |
| v0.11.1 | 2026-10-11 | [38142818146](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38142818146) | Real-Azure certification: three true-streaming e2e tests failed against the deployed test app. Two received HTTP 404 where they expected 200, and the disconnect test found no first frame (`StopIteration`). #589 followed, and v0.11.2 failed the same three tests. | 0.11.4 |
| v0.11.2 | 2026-10-11 | [38144369700](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38144369700) | Real-Azure certification: the same three tests failed in the same ways. #591 followed, and v0.11.3 failed the same three tests. | 0.11.4 |
| v0.11.3 | 2026-10-11 | [38145575218](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38145575218) | Real-Azure certification: the same three tests failed in the same ways. #593 followed, and 0.11.4 passed every gate. | 0.11.4 |
| v0.12.1 | 2026-10-16 | [38891888298](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38891888298) | Installed-wheel tests: `No module named 'scripts'` while collecting `tests/test_example_install_certification.py`. #636 followed, and 0.12.2 passed every gate. | 0.12.2 |

How to read the table:

- Every row is a failed `Publish to PyPI` run, none of these versions has files on PyPI as of the
  verification date, and none of the tags lacks a publish run.
- The first published successor is the first published version whose tag commit descends from the
  unpublished tag. Every published version after it, through v0.13.0, descends from it as well. That
  does not guarantee unchanged APIs or behavior, so read the release notes before moving. For a new
  install use the latest release.
- GitHub keeps job logs for a limited time. By the verification date the logs of the April 2026 runs had
  expired, so those rows rest on the run record, the check annotations that GitHub still keeps and the
  git history. The October rows rest on the job logs.
- There is no v0.6.x: no tag, GitHub Release or PyPI version exists for it.

## What to do

- **New installs from PyPI:** use the latest version that PyPI lists.
- **An exact pin to a version above**, for example `azure-functions-langgraph==0.12.1`, cannot be
  resolved from PyPI. Pin the first published successor, or use a range that contains published versions.
- **Source taken from one of these tags** (a git checkout, `pip install git+https://...@<tag>`, or the
  GitHub source archive) comes from a tag whose publish run failed, and it is not a PyPI distribution.
  Prefer a published successor.

## Historical disposition and recovery policy

As of the verification date none of the versions above has been published, and the published versions
that follow them carry their changes. [Release Process](release_process.md#recovery) defines how such a
tag is handled, and this page does not add to it:

- A tag that was never published is a valid resting state.
- The workflow may be re-run on the unchanged tag after a transient or external failure.
- A fix to the source or to the workflow needs a new commit and a new release; it is never applied to
  an already-cut tag.
- A tag is never moved or reused.
- `publish-pypi.yml` is the only path that uploads to PyPI, so a file reaches PyPI only through the
  gated run for its tag.

The reasons: a tag names the exact commit that was built and tested. Moving it to another commit would
change what readers, and anything that pinned the tag or its GitHub Release, refer to, and the tag, the
GitHub Release and any PyPI file would stop describing the same source. Uploading outside that workflow
would bypass the repository's required publication path and its enforced gate dependencies, even if the
files had been tested separately.
The GitHub Releases stay as the record of what each tag contained, and their status is stated here
instead of being hidden by deleting them.

## Changelog and release notes

`CHANGELOG.md` and the GitHub Release notes describe the changes of a version. They do not say whether
it was published. Release Please writes them for current releases; earlier releases used the previous
tooling. All the versions above have a changelog entry and a GitHub Release, so use this page or PyPI to
see what was published.

## Related

- [Release Process](release_process.md): how a version reaches PyPI, and the recovery rules.
- [Changelog](changelog.md)
- [Installation](installation.md)
