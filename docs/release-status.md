# Release status

A git tag or a GitHub Release does **not** mean that a version can be installed.
`azure-functions-langgraph` is distributed through
[PyPI](https://pypi.org/project/azure-functions-langgraph/), and only a version that has files there
can be installed with `pip install azure-functions-langgraph==<version>`.

This page lists the tagged versions that never reached PyPI, what stopped each one, and which version
to use instead. In every case the `Publish to PyPI` run for the tag failed and uploaded nothing; the
cause was then resolved and a later version was published.

*Last verified: 2026-11-10, against PyPI, the GitHub Releases API, the git tags and the GitHub Actions
run records. It covers the GitHub Releases from v0.2.0 to v0.13.0.*

## Why a GitHub Release can exist without a PyPI file

1. When the Release PR is merged, Release Please creates the tag and the GitHub Release.
2. The tag starts `publish-pypi.yml`: build, library tests, installed-wheel tests, runtime audit and
   real-Azure certification, then `publish`. Only the `publish` job uploads to PyPI.
3. If a gate fails, `publish` is skipped and nothing is uploaded. The GitHub Release stays, because it
   was created before the gates ran and no workflow removes it.

Releases up to v0.8.2 were created by a separate `Create GitHub Release` workflow on the tag push,
which did not wait for the publish run either.

A GitHub Release is therefore the record of a release candidate. Whether that candidate was distributed
is a separate fact, and PyPI is where to check it.

## Check whether a version is installable

- The [PyPI release history](https://pypi.org/project/azure-functions-langgraph/#history) is the
  authority.
- For scripts, `https://pypi.org/pypi/azure-functions-langgraph/<version>/json` answers `200` for a
  version that has files and `404` for one that has none.
- `pip install azure-functions-langgraph==<version>` for a version without files fails with
  `No matching distribution found`.

A range such as `azure-functions-langgraph>=0.12.0,<0.13` is not affected, because pip chooses among the
versions that PyPI has. Only an exact pin to one of the versions below, or an install from the tag or
from the GitHub source archive, is.

## Tagged versions without a PyPI distribution

| Version | GitHub Release | Publish run | What stopped the upload | Successor on PyPI |
| --- | --- | --- | --- | --- |
| v0.2.0 | 2026-04-04 | [23981822989](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23981822989) | Publishing: PyPI found no trusted publisher that matched the workflow (`invalid-publisher`), so the token exchange was refused. | 0.5.0 |
| v0.3.0 | 2026-04-04 | [23984989252](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23984989252) | Publishing: the same `invalid-publisher` refusal. | 0.5.0 |
| v0.4.0 | 2026-04-05 | [23993056592](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/23993056592) | Publishing: the same `invalid-publisher` refusal. The publish workflow file is identical at v0.4.0 and v0.5.0, and the first file on PyPI is 0.5.0, uploaded on 2026-04-06, so what changed was on the PyPI side, outside this repository. | 0.5.0 |
| v0.5.2 | 2026-04-18 | [24605716212](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/24605716212) | Tag gate: `Version in __init__.py (0.5.1) does not match git tag (0.5.2)`. | 0.5.4 |
| v0.5.3 | 2026-04-26 | [24958757167](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/24958757167) | Publishing: the PyPI publish action failed; its attestation notice is the last annotation that survives and the log has expired, so the exact error cannot be retrieved. The likely cause, not confirmed by a log: the tagged `pyproject.toml` named the distribution `azure-functions-langgraph-python`, and no such project exists on PyPI. 0.5.4 renamed it back to `azure-functions-langgraph`. | 0.5.4 |
| v0.11.0 | 2026-10-11 | [38141259504](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38141259504) | Two gates. Installed-wheel tests: `No module named 'scripts'` while collecting `tests/test_check_audit_exceptions.py`. Real-Azure certification: the remote build of the test app failed because it pinned `azure-functions-langgraph<0.11` while the candidate wheel was 0.11.0. Fixed forward in #587. | 0.11.4 |
| v0.11.1 | 2026-10-11 | [38142818146](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38142818146) | Real-Azure certification: three true-streaming e2e tests failed; the deployed test app answered HTTP 404 where the tests expected 200. Fixed forward in #589. | 0.11.4 |
| v0.11.2 | 2026-10-11 | [38144369700](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38144369700) | Real-Azure certification: the same three true-streaming e2e tests failed with the same 404. Fixed forward in #591. | 0.11.4 |
| v0.11.3 | 2026-10-11 | [38145575218](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38145575218) | Real-Azure certification: the same three true-streaming e2e tests failed with the same 404. Fixed forward in #593, which 0.11.4 carries. | 0.11.4 |
| v0.12.1 | 2026-10-16 | [38891888298](https://github.com/yeongseon/azure-functions-langgraph-python/actions/runs/38891888298) | Installed-wheel tests: `No module named 'scripts'` while collecting `tests/test_example_install_certification.py`. Fixed forward in #636. | 0.12.2 |

How to read the table:

- Every row is a failed `Publish to PyPI` run. Either a gate failed and `publish` was skipped, or the
  upload itself was refused. Nothing was uploaded for any of these versions, and none of the tags lacks a
  publish run.
- The successor is the first version on PyPI whose history contains the tag, so it carries the
  tag's changes; so does every later version. For a new install use the latest release.
- GitHub keeps job logs for a limited time. By the verification date the logs of the April 2026 runs had
  expired, so those rows rest on the run record, the check annotations that GitHub still keeps and the
  git history. The October rows rest on the job logs.
- There is no v0.6.x: no tag, GitHub Release or PyPI version exists for it.

## What to do

- **New installs:** use the latest version that PyPI lists.
- **An exact pin to a version above**, for example `azure-functions-langgraph==0.12.1`, cannot be
  installed. Pin the successor, or use a range such as `>=0.12.0,<0.13`.
- **A copy of the source taken from one of these tags** (a git checkout, the GitHub source archive, or
  `pip install git+https://...@<tag>`) is not a release: the release workflow for that tag did not
  complete. Move to the successor.

## Why the tags stay and the versions are not backfilled

Each of these versions was handled by moving forward, as [Release Process](release_process.md#recovery)
describes: the cause was resolved (in the code on `main`, or for the first three in the PyPI publisher
setup), and the next release that passed its gates carried the fix. The project does not move or reuse
a tag and does not upload a distribution for a tag that did not complete its release workflow:

- A tag names the exact commit that was built and tested. Moving it to another commit would change what
  readers, and anything that pinned the tag or its GitHub Release, refer to, and the tag, the GitHub
  Release and any later PyPI file would stop describing the same source.
- For these versions the cause lay in the tagged source or in the release setup of that time, so
  running the workflow again on the old tag could not succeed, and building a different artifact under
  the old version number would not be what the tag names.
- The GitHub Releases stay as the record of what each tag contained. Their status is stated here and
  is not hidden by deleting them.

## Changelog and release notes

[`CHANGELOG.md`](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CHANGELOG.md)
is written by Release Please from the commits of each tag, and the GitHub Release notes list the same
changes. Both describe what a tag contains and not whether it was published, so the versions above
appear in both. Use this page or PyPI for that.

## Related

- [Release Process](release_process.md): how a version reaches PyPI, and the recovery rules.
- [Changelog](changelog.md)
- [Installation](installation.md)
