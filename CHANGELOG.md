# Changelog

All notable changes to this project will be documented in this file.
## [0.9.4](https://github.com/yeongseon/azure-functions-langgraph-python/compare/v0.9.3...v0.9.4) (2026-10-05)


### Bug Fixes

* **deps:** align azure-functions floor at 1.21.0 ([#548](https://github.com/yeongseon/azure-functions-langgraph-python/issues/548)) ([b95b866](https://github.com/yeongseon/azure-functions-langgraph-python/commit/b95b8669d4350c11c4528be93f284277c6d1c895))

## [0.9.3](https://github.com/yeongseon/azure-functions-langgraph-python/compare/v0.9.2...v0.9.3) (2026-10-03)


### Bug Fixes

* **durable:** merge thread_id into non-empty run config ([#535](https://github.com/yeongseon/azure-functions-langgraph-python/issues/535)) ([002c713](https://github.com/yeongseon/azure-functions-langgraph-python/commit/002c713242081a81285c93a3612979295de36ffe))
* **durable:** sanitize backend errors in run status ([#534](https://github.com/yeongseon/azure-functions-langgraph-python/issues/534)) ([589f7dc](https://github.com/yeongseon/azure-functions-langgraph-python/commit/589f7dc600fbfece3006c196b1fd696afc3f01ab))
* **platform:** reject unknown stream modes ([#537](https://github.com/yeongseon/azure-functions-langgraph-python/issues/537)) ([1c0c6e0](https://github.com/yeongseon/azure-functions-langgraph-python/commit/1c0c6e0913b8bd4bb8195fae618f6bab45c99367))
* **platform:** share the thread lock with native routes ([#532](https://github.com/yeongseon/azure-functions-langgraph-python/issues/532)) ([ad3af36](https://github.com/yeongseon/azure-functions-langgraph-python/commit/ad3af36894d994f58d415a7fb44db1430b730e20))
* **streaming:** honor sync mode with SQLite checkpoints ([#536](https://github.com/yeongseon/azure-functions-langgraph-python/issues/536)) ([59dd51d](https://github.com/yeongseon/azure-functions-langgraph-python/commit/59dd51ddf7916d4810526b4836e0728f6c688f00))
* **streaming:** serialize LangChain messages as JSON objects ([#538](https://github.com/yeongseon/azure-functions-langgraph-python/issues/538)) ([db430fa](https://github.com/yeongseon/azure-functions-langgraph-python/commit/db430fada649b643340532504c5ee313a61847fa))

## [0.9.2](https://github.com/yeongseon/azure-functions-langgraph-python/compare/v0.9.1...v0.9.2) (2026-10-02)


### Bug Fixes

* **ci:** trust the Cosmos emulator certificate ([#523](https://github.com/yeongseon/azure-functions-langgraph-python/issues/523)) ([d2b356f](https://github.com/yeongseon/azure-functions-langgraph-python/commit/d2b356fb9f2e31a1d29aacb65662039996123205))
* **native:** require thread id for checkpointed graphs ([#517](https://github.com/yeongseon/azure-functions-langgraph-python/issues/517)) ([ad225dc](https://github.com/yeongseon/azure-functions-langgraph-python/commit/ad225dcc62fdb58ff95ca00404dc63f37d87db28))
* **streaming:** preserve overflow terminal framing ([#518](https://github.com/yeongseon/azure-functions-langgraph-python/issues/518)) ([d033656](https://github.com/yeongseon/azure-functions-langgraph-python/commit/d0336561d5bd0280f705ac65a45ad7569a79329b))

## [0.9.1](https://github.com/yeongseon/azure-functions-langgraph-python/compare/v0.9.0...v0.9.1) (2026-10-01)


### Bug Fixes

* **compat:** deprecate Python 3.10 ahead of its removal ([#510](https://github.com/yeongseon/azure-functions-langgraph-python/issues/510)) ([0ba4398](https://github.com/yeongseon/azure-functions-langgraph-python/commit/0ba4398d7b7f1fc52447f795f687fa3acba297ef))
* **streaming:** allow exactly max_stream_events frames ([#511](https://github.com/yeongseon/azure-functions-langgraph-python/issues/511)) ([7a7cfd3](https://github.com/yeongseon/azure-functions-langgraph-python/commit/7a7cfd310054b93475400bcf75e8c0e7319f71ec))

## [0.9.0](https://github.com/yeongseon/azure-functions-langgraph-python/compare/v0.8.2...v0.9.0) (2026-09-29)


### Features

* **app:** reject async-only graphs under platform_compat ([#445](https://github.com/yeongseon/azure-functions-langgraph-python/issues/445)) ([677c4a8](https://github.com/yeongseon/azure-functions-langgraph-python/commit/677c4a853da80759dd1ee802391324f4fb0895a6)), closes [#442](https://github.com/yeongseon/azure-functions-langgraph-python/issues/442)
* **checkpointer:** add native async blob I/O to AzureBlobCheckpointSaver ([#440](https://github.com/yeongseon/azure-functions-langgraph-python/issues/440)) ([5ae92f2](https://github.com/yeongseon/azure-functions-langgraph-python/commit/5ae92f2d7e181738412e8a7b00d91c9b7bff3987))
* **deps:** lift azure-functions &lt;2.0.0 cap on Python 3.13+ ([#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350)) ([#457](https://github.com/yeongseon/azure-functions-langgraph-python/issues/457)) ([2649047](https://github.com/yeongseon/azure-functions-langgraph-python/commit/26490473214add7ad0093481e80080bf5ce483b4))
* **observability:** add optional OpenTelemetry RunObserver ([#446](https://github.com/yeongseon/azure-functions-langgraph-python/issues/446)) ([86c65c5](https://github.com/yeongseon/azure-functions-langgraph-python/commit/86c65c56c236c292fe45c9783ca807ff24981ecb)), closes [#434](https://github.com/yeongseon/azure-functions-langgraph-python/issues/434)
* optional Durable Functions async run lifecycle ([#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408)) ([#453](https://github.com/yeongseon/azure-functions-langgraph-python/issues/453)) ([66b9093](https://github.com/yeongseon/azure-functions-langgraph-python/commit/66b909370cd316eaef19ad1db8466eece3e63162))
* **streaming:** add opt-in true HTTP streaming via StreamingLangGraphApp ([#449](https://github.com/yeongseon/azure-functions-langgraph-python/issues/449)) ([c824020](https://github.com/yeongseon/azure-functions-langgraph-python/commit/c82402024d99702e902ea43fa14cc414793672a8)), closes [#406](https://github.com/yeongseon/azure-functions-langgraph-python/issues/406)
* **triggers:** add Azure Service Bus trigger adapter ([#409](https://github.com/yeongseon/azure-functions-langgraph-python/issues/409)) ([#448](https://github.com/yeongseon/azure-functions-langgraph-python/issues/448)) ([f402d1f](https://github.com/yeongseon/azure-functions-langgraph-python/commit/f402d1f97febd40fef452f2f83722797749d2a9c))


### Bug Fixes

* **ci:** correct the repository guard and align stale.yml with the fleet ([#483](https://github.com/yeongseon/azure-functions-langgraph-python/issues/483)) ([cd1f837](https://github.com/yeongseon/azure-functions-langgraph-python/commit/cd1f837d3023bcf67e2858b38d1f67e6e5a1c35e))
* **ci:** run the test matrix on each interpreter, not just Python 3.10 ([#436](https://github.com/yeongseon/azure-functions-langgraph-python/issues/436)) ([2963983](https://github.com/yeongseon/azure-functions-langgraph-python/commit/29639833f43fa4bca232e1c98dd510ce8873ce74)), closes [#427](https://github.com/yeongseon/azure-functions-langgraph-python/issues/427)
* **ci:** stop the changed-file format gate failing open ([#479](https://github.com/yeongseon/azure-functions-langgraph-python/issues/479)) ([cf386b0](https://github.com/yeongseon/azure-functions-langgraph-python/commit/cf386b0bf67a4ee89b6f544cb51229ab59a61d85))
* **ci:** unbreak main — canonical azure/login pin and observability_otel typing ([#475](https://github.com/yeongseon/azure-functions-langgraph-python/issues/475)) ([061e514](https://github.com/yeongseon/azure-functions-langgraph-python/commit/061e514946642af83a746d1202fd06f9cefff160))
* **deps:** drop unsupported semver cooldown keys for github-actions ecosystem ([#455](https://github.com/yeongseon/azure-functions-langgraph-python/issues/455)) ([e929d37](https://github.com/yeongseon/azure-functions-langgraph-python/commit/e929d3768775cbf9e7b846eee8a4b02b4b338730))
* **locks:** make AzureBlobLeaseThreadLock.close() terminal so acquire() after close raises ([#461](https://github.com/yeongseon/azure-functions-langgraph-python/issues/461)) ([b8538ad](https://github.com/yeongseon/azure-functions-langgraph-python/commit/b8538ad33fec7fd1e94bf485c3ae671249b06a5d))
* **templates:** use Conventional Commit prefixes in issue forms ([#487](https://github.com/yeongseon/azure-functions-langgraph-python/issues/487)) ([e37b636](https://github.com/yeongseon/azure-functions-langgraph-python/commit/e37b6360b7a7abc5f610e2a496b6be4e80cb1b04))


### Documentation

* adopt rolling 0.x per-surface stability policy ([#444](https://github.com/yeongseon/azure-functions-langgraph-python/issues/444)) ([2602e06](https://github.com/yeongseon/azure-functions-langgraph-python/commit/2602e062cb5d5289bff634b11649bd591b1333bd)), closes [#426](https://github.com/yeongseon/azure-functions-langgraph-python/issues/426)
* align the contributor contract with the actual configuration ([#481](https://github.com/yeongseon/azure-functions-langgraph-python/issues/481)) ([6eb369d](https://github.com/yeongseon/azure-functions-langgraph-python/commit/6eb369dfda9211976faedb6745670aa0a602686f))
* **ci:** state the real azure-functions cap and certification status ([#472](https://github.com/yeongseon/azure-functions-langgraph-python/issues/472)) ([be164c7](https://github.com/yeongseon/azure-functions-langgraph-python/commit/be164c7d69b3b3ac78590554532441b7f3d7f64a))
* **contributing:** correct the release description ([#485](https://github.com/yeongseon/azure-functions-langgraph-python/issues/485)) ([146b35b](https://github.com/yeongseon/azure-functions-langgraph-python/commit/146b35b43d285efba884820814f5e0254f1f8950)), closes [#484](https://github.com/yeongseon/azure-functions-langgraph-python/issues/484)
* **roadmap:** add adoption-first rolling roadmap page ([#410](https://github.com/yeongseon/azure-functions-langgraph-python/issues/410)) ([#451](https://github.com/yeongseon/azure-functions-langgraph-python/issues/451)) ([d44d36c](https://github.com/yeongseon/azure-functions-langgraph-python/commit/d44d36cc8bfacb5787f48009788b233bcdfcdbd1))
* **roadmap:** mark Durable async runs ([#408](https://github.com/yeongseon/azure-functions-langgraph-python/issues/408)) landed; reach expansion checkpoint ([#456](https://github.com/yeongseon/azure-functions-langgraph-python/issues/456)) ([9a58a82](https://github.com/yeongseon/azure-functions-langgraph-python/commit/9a58a82ad12f2b3612349a716dfc2e9ae9d8ad9a))
* **roadmap:** reflect [#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350) azure-functions 2.x cap lifted on Py 3.13+ via [#457](https://github.com/yeongseon/azure-functions-langgraph-python/issues/457) ([#458](https://github.com/yeongseon/azure-functions-langgraph-python/issues/458)) ([7589694](https://github.com/yeongseon/azure-functions-langgraph-python/commit/7589694db5cfb2b71a4c78654a0c47bac58e5ea6))
* roll changelog Unreleased into 0.8.2 heading ([ec63741](https://github.com/yeongseon/azure-functions-langgraph-python/commit/ec637419907b41c8c4f14bd4402ef139a3c6db24))
* stop claiming the azure-functions 2.x path is certified ([#480](https://github.com/yeongseon/azure-functions-langgraph-python/issues/480)) ([f5450d7](https://github.com/yeongseon/azure-functions-langgraph-python/commit/f5450d7a15b6a16ea7495f883cad47147fe0fa7f))


### Miscellaneous Tasks

* add hatch-matrix hygiene lint to guard the CI matrix ([#438](https://github.com/yeongseon/azure-functions-langgraph-python/issues/438)) ([8937610](https://github.com/yeongseon/azure-functions-langgraph-python/commit/89376102877c9180c2f1f371d0b1d5c7b0f6de18)), closes [#437](https://github.com/yeongseon/azure-functions-langgraph-python/issues/437)
* adopt release-please and gate PyPI on in-chain Azure e2e ([#495](https://github.com/yeongseon/azure-functions-langgraph-python/issues/495)) ([12519c2](https://github.com/yeongseon/azure-functions-langgraph-python/commit/12519c2c81f2c77b1d5aa57745dcc4a6881a3711))
* allow build/ branch prefix in branch-naming validation ([#454](https://github.com/yeongseon/azure-functions-langgraph-python/issues/454)) ([884b5d1](https://github.com/yeongseon/azure-functions-langgraph-python/commit/884b5d131c7802737dbae3b73ddd3a859e8bfcdd))
* **deps:** align langgraph-sdk max lane to the real &lt;0.5 ceiling ([#443](https://github.com/yeongseon/azure-functions-langgraph-python/issues/443)) ([c84ec2e](https://github.com/yeongseon/azure-functions-langgraph-python/commit/c84ec2e040889b4be2af12461ddd2176109be6a1)), closes [#441](https://github.com/yeongseon/azure-functions-langgraph-python/issues/441)
* **deps:** bump codecov/codecov-action in the github-actions group ([#467](https://github.com/yeongseon/azure-functions-langgraph-python/issues/467)) ([5c59d6d](https://github.com/yeongseon/azure-functions-langgraph-python/commit/5c59d6db7e4b611bb6b3be88881c5cd0012647a7))
* **deps:** bump ruff in the python-dependencies group ([#447](https://github.com/yeongseon/azure-functions-langgraph-python/issues/447)) ([0bfb8e6](https://github.com/yeongseon/azure-functions-langgraph-python/commit/0bfb8e6069fab84bc5040d0696876d170bf54063))
* **deps:** bump ruff in the python-dependencies group ([#462](https://github.com/yeongseon/azure-functions-langgraph-python/issues/462)) ([399c136](https://github.com/yeongseon/azure-functions-langgraph-python/commit/399c136c5e64ac540939ffe8e08a5f06e4909f38))
* **deps:** bump ruff in the python-dependencies group ([#466](https://github.com/yeongseon/azure-functions-langgraph-python/issues/466)) ([c9de4b6](https://github.com/yeongseon/azure-functions-langgraph-python/commit/c9de4b63fa5da99ac63a048dcd17e05aee73b949))
* **deps:** bump the github-actions group with 3 updates ([#463](https://github.com/yeongseon/azure-functions-langgraph-python/issues/463)) ([092a90d](https://github.com/yeongseon/azure-functions-langgraph-python/commit/092a90d9843f649ab578d5a23b481adf646367fe))
* **deps:** certify azure-functions 2.x on Py 3.14 and draft Flex Consumption e2e ([#450](https://github.com/yeongseon/azure-functions-langgraph-python/issues/450)) ([fd814eb](https://github.com/yeongseon/azure-functions-langgraph-python/commit/fd814eb5eeacb8842b0c5d4687ac583d08412905)), closes [#350](https://github.com/yeongseon/azure-functions-langgraph-python/issues/350)
* enforce Ruff formatting in PR quality checks ([#470](https://github.com/yeongseon/azure-functions-langgraph-python/issues/470)) ([3cad811](https://github.com/yeongseon/azure-functions-langgraph-python/commit/3cad811c6b64ccf832afda12594319f9d1de0d11))
* ignore uv.lock ([#491](https://github.com/yeongseon/azure-functions-langgraph-python/issues/491)) ([4e2cb8f](https://github.com/yeongseon/azure-functions-langgraph-python/commit/4e2cb8f0b953512f4980824db3dcc5b7e6dbe492)), closes [#490](https://github.com/yeongseon/azure-functions-langgraph-python/issues/490)
* modernize typing and pin ruff, complete AGENTS.md, unify Azure e2e auth ([#493](https://github.com/yeongseon/azure-functions-langgraph-python/issues/493)) ([6aeb6b4](https://github.com/yeongseon/azure-functions-langgraph-python/commit/6aeb6b44b3862385c4ac76a2fbd2ee612c0a6a62))
* wire and harden the hatch default-env-pin lint ([#452](https://github.com/yeongseon/azure-functions-langgraph-python/issues/452)) ([a2fbf12](https://github.com/yeongseon/azure-functions-langgraph-python/commit/a2fbf12a3e5ee03dd0ab978d70b27e52b25c3425))

## [0.8.2] - 2026-08-14

### ⚙️ Miscellaneous Tasks

- Stop auto-deploying docs to GitHub Pages (#332) 
- Add a wheel-tests tier that runs lib-tests against the built wheel (#326) 
- Adopt Renovate for GitHub Actions bumps (#324) 
- Ignore agent orchestration state (.sisyphus/, .omc/) (#307) 
- *(ci)* Normalize action version-comment labels (#322) 
- Add workflow pin-hygiene lint (#320) 
- Add release-gate workflow drift lint (#316) 
- Normalize release-gate action pins to canonical SHAs (#314) 
- Add tiered pre-publish runtime gate with real-Azure certification (#305) 
- Drop stale noqa directives (#304) 

### 💼 Other

- Bump version to 0.8.2 

### 📚 Documentation

- Consolidate official documentation URL onto yeongseon.dev (#327) 
- Mark package Experimental in README (en/ko/ja/zh-CN) (#309) 
- *(i18n)* Adopt best-effort translation policy with staleness banners (#318) 
## [0.8.1] - 2026-08-11

### 💼 Other

- Bump version to 0.8.1 

### 📚 Documentation

- Update changelog 
- Add Branch Hygiene section to AGENTS.md 
- *(release)* Require cookbook dogfood verification after publish 
- *(endpoint)* Reframe SPEC-pinned wording as an independent convention choice (#303) 
## [0.8.0] - 2026-08-09

### ⚙️ Miscellaneous Tasks

- *(deps)* Cap azure-functions below 2.0.0 (#301) 
- *(deps)* Bump github/codeql-action/analyze from 4.37.4 to 4.37.6 (#298) 
- *(deps)* Bump github/codeql-action/init from 4.37.4 to 4.37.6 (#297) 
- *(deps)* Bump ruff from 0.16.0 to 0.16.1 (#296) 
- *(deps)* Bump codeql-action init+analyze to 4.37.4 atomically 
- *(deps)* Bump ruff from 0.15.22 to 0.16.0 (#290) 
- *(deps)* Bump actions/stale from 10.4.0 to 11.0.0 (#291) 
- Track issue priority via priority:* labels instead of body line (#293) 
- *(deps)* Bump github/codeql-action/init from 4.37.1 to 4.37.3 (#285) 
- *(deps)* Bump actions/checkout from 7.0.0 to 7.0.1 (#287) 
- *(deps)* Bump actions/setup-python from 6.3.0 to 7.0.0 (#286) 

### 🐛 Bug Fixes

- *(deps)* Pin azure-functions to >=1.17,<3 (#300) 

### 💼 Other

- Bump version to 0.8.0 

### 📚 Documentation

- Update changelog 
- Require translation sync in the same PR as English changes (Closes #283) (#284) 
- Align codecov badge slug and renumber SDK endpoint list (#282) 
- Correct azure-functions-db description in ecosystem table (#280) 

### 🚀 Features

- *(endpoint)* Write shared endpoint metadata namespace (#294) (#295) 

### 🚜 Refactor

- *(handlers)* Extract shared native request parser (#278) 
- *(platform)* Extract shared SSE overflow guard in _runs.py (#277) 
- *(platform)* De-duplicate thread route preambles in _threads.py (#276) 
- *(platform)* De-duplicate RunCreate preamble in _runs.py (#275) 
- *(metadata)* Type the langgraph cross-package metadata contract (#274) 
- *(platform)* Extract shared SSE response + stream_mode helpers (#273) 
## [0.7.3] - 2026-07-18

### Diagram

- Add SSE overflow path, thread state machine, and lease-lifecycle diagrams (#267) 

### ⚙️ Miscellaneous Tasks

- *(deps)* Bump github/codeql-action/analyze from 4.36.3 to 4.37.1 (#260) 
- *(deps)* Bump github/codeql-action/init from 4.36.3 to 4.37.1 (#258) 
- *(app)* Dedupe route registration, single-source capability gates, table-driven lazy imports (#270) 
- Document coverage-gate vs compat-smoke split in ci-test.yml (#268) 
- *(deps)* Bump actions/stale from 10.3.0 to 10.4.0 (#259) 
- *(deps)* Bump softprops/action-gh-release from 3.0.1 to 3.0.2 (#261) 
- *(deps)* Bump ruff from 0.15.20 to 0.15.22 (#262) 
- *(deps)* Bump mypy from 2.1.0 to 2.3.0 (#263) 
- *(ci)* Pin action-gh-release to commit SHA and document policy (#246) 
- *(deps)* Bump github/codeql-action from 4.36.2 to 4.36.3 (init+analyze) (#239) 
- *(deps)* Bump actions/checkout from 6.0.3 to 7.0.0 (#232) 
- *(deps)* Bump actions/setup-python from 6.2.0 to 6.3.0 (#234) 
- *(deps)* Bump ruff from 0.15.15 to 0.15.20 (#238) 
- *(deps)* Bump codecov/codecov-action from 6.0.1 to 7.0.0 (#228) 
- *(deps)* Bump github/codeql-action from 4.35.4 to 4.36.2 (#227) 
- *(deps)* Bump actions/checkout from 6.0.2 to 6.0.3 (#226) 
- *(deps)* Bump ruff from 0.15.12 to 0.15.15 (#225) 
- *(deps)* Update langgraph-sdk requirement (#224) 
- *(deps)* Bump actions/stale from 10.2.0 to 10.3.0 (#220) 
- *(deps)* Bump codecov/codecov-action from 6.0.0 to 6.0.1 (#219) 
- *(deps)* Bump mypy from 2.0.0 to 2.1.0 (#218) 

### 🐛 Bug Fixes

- *(app)* Default auth_level to FUNCTION; align Alpha maturity claims (#243) 
- *(ci)* Bypass SSL verification for Cosmos emulator in create_cosmos_checkpointer 

### 💼 Other

- Bump version to 0.7.3 

### 📚 Documentation

- Update changelog 
- Consolidate thread-lock API, SSE semantics, and RunCreate status mapping (#266) 
- Add discoverability metadata (pepy badge + llms.txt) (#272) 
- Document Release Process in AGENTS.md (#255) 
- *(locks)* Warn that AzureBlobLeaseThreadLock does not renew Azure Blob leases (#248) 

### 🚀 Features

- *(locks)* Add background auto-renewal to AzureBlobLeaseThreadLock (#250) 
- *(locks)* Pluggable ThreadLock backend with Azure Blob lease implementation (#244) 
## [0.7.2] - 2026-05-14

### ⚙️ Miscellaneous Tasks

- *(release)* Fix changelog template and decouple version test from literals 

### 💼 Other

- Bump version to 0.7.2 

### 📚 Documentation

- Update changelog 
## [0.7.1] - 2026-05-12

### ⚙️ Miscellaneous Tasks

- *(examples)* Sync requirements pins and remove pre-release notes for v0.7.1 (#216) 
- *(release)* Prepare v0.7.1 with documentation parity fixes (#215) 
- *(cosmos)* Move cosmos integration to scheduled + workflow_dispatch only (#206) 
- *(table)* Azurite-backed integration tests + route_prefix docstring (#199) 
- *(deps)* Bump mypy from 1.20.2 to 2.0.0 (#193) 
- *(deps)* Bump github/codeql-action from 4.35.2 to 4.35.4 

### 🎨 Styling

- Fix lint and typecheck errors (#194) 

### 🐛 Bug Fixes

- *(handlers)* Validate thread_id via validate_thread_id in _extract_thread_id 
- *(handlers)* Prevent lock cleanup race and metadata path double slash 
- *(handlers)* Harden native endpoints — configurable type guard, lock cleanup, route normalization 

### 📚 Documentation

- *(checkpointers)* Note Cosmos Managed Identity is unsupported by upstream (#208) (#214) 
- *(readme)* Clarify LangGraphApp.route_prefix is metadata-only (#207) (#213) 
- *(stores)* Document reset_stale_locks projection/ETag skip behavior (#210) (#212) 
- *(readme)* Note native endpoint thread lock is not distributed (#209) (#211) 
- *(readme)* Translate health_auth_level ANONYMOUS-default warning into ko/ja/zh-CN (#205) 
- *(examples)* Add explicit health_auth_level to production examples 

### 🚀 Features

- *(app)* Add health_auth_level param; fix Makefile cleanup; add Table integration tests 

### 🧪 Testing

- *(stores)* Assert projection query returns usable ETag against Azurite (#204) 
## [0.7.0] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Bump version to 0.7.0 and update CHANGELOG 
- *(deps)* Bump mypy from 1.20.1 to 1.20.2 (#137) 
- Apply ruff format, fix description and docs for examples (#171) 
- *(deps)* Bump ruff from 0.15.10 to 0.15.12 (#166) 

### 🐛 Bug Fixes

- Resolve mypy errors in cosmos helper tests 
- *(checkpointers)* Align cosmos helper with upstream CosmosDBSaver API 
- Resolve ruff lint errors (import sorting, unused variable) 
- *(checkpointers)* Use exact version range in cosmos import error message 
- *(checkpointers)* Align cosmos dependency with actual upstream package name 
- Remove leftover Python 3.11 and DefaultAzureCredential references in cosmos example 
- *(checkpointers)* Make close_cosmos_checkpointer idempotent and harden CI 

### 📚 Documentation

- Fix ecosystem table names, badges, and Part of intro line 
- Mark cookbook as dogfood, fix ecosystem table description 
- *(examples)* Add Managed Identity walkthrough for Blob + Table backends (#165) 
- *(retention)* Clarify delete_old_checkpoints leaves orphaned channel value blobs (#154) (#159) 

### 🚀 Features

- *(checkpointers)* Add close_cosmos_checkpointer cleanup helper 
- *(checkpointers)* Add safe garbage collection for orphaned Azure Blob channel values (#153) (#160) 
- *(stores)* Add reset_stale_locks() to AzureTableThreadStore (#170) 
- *(checkpointers)* Add experimental Cosmos DB checkpointer helper (#169) 
- *(stores)* Add AzureTableThreadStore.from_table_client factory (#161) 
- *(checkpointers)* Add Postgres and SQLite DB checkpointer DX helpers (#163) 

### 🧪 Testing

- Raise coverage to 95%+ and enforce via AGENTS.md and pyproject.toml 
- *(checkpointers)* Add Cosmos DB emulator integration tests (#167) 
## [0.5.4] - 2026-04-27

### 🐛 Bug Fixes

- *(packaging)* Rename PyPI distribution back to azure-functions-langgraph 

### 📚 Documentation

- Update changelog 
## [0.5.3] - 2026-04-26

### ⚙️ Miscellaneous Tasks

- *(deps)* Pin langgraph to >=1.0,<2.0 with min-version CI compat job (#145) (#152) 

### 🐛 Bug Fixes

- *(stores)* Add atomic run lock to ThreadStore for safe concurrent runs (#142) (#149) 
- Declare wheel packages explicitly for hatchling (#138) 

### 💼 Other

- Bump version to 0.5.3 

### 📚 Documentation

- Update changelog 
- *(examples)* Add platform-SDK, persistent storage, OpenAPI, auth, and curl examples (#144) (#151) 
- Add per-feature SDK compatibility matrix (#141) (#148) 
- Clarify buffered SSE behavior for all stream endpoints (#147) 
- Drop stale beta version and fix metadata API name in READMEs (#146) 
- *(agents)* Add Issue Conventions section to AGENTS.md 

### 🚀 Features

- *(checkpointers)* Add retention helpers and document scale envelope (#143) (#150) 
## [0.5.2] - 2026-04-17

### ⚙️ Miscellaneous Tasks

- *(deps)* Bump softprops/action-gh-release from 2 to 3 
- *(deps)* Bump mypy from 1.20.0 to 1.20.1 
- *(deps)* Bump actions/upload-artifact from 7.0.0 to 7.0.1 
- *(deps)* Bump github/codeql-action from 4.35.1 to 4.35.2 
- *(deps)* Bump actions/github-script from 8.0.0 to 9.0.0 
- Update repo references for azure-functions-{feature}-python naming convention 
## [0.5.1] - 2026-04-10

### ⚙️ Miscellaneous Tasks

- Align config and docs with canonical DX Toolkit template (#128) 
- *(deps)* Bump ruff from 0.15.8 to 0.15.10 (#124) 

### 💼 Other

- Bump version to 0.5.1 

### 📚 Documentation

- Update changelog 
- Add ecosystem table to README 
- Add llms.txt for LLM-friendly documentation (#120) (#121) 

### 🚀 Features

- Add toolkit metadata convention support 

### 🚜 Refactor

- Rename metadata attr to _azure_functions_metadata (#130) 
## [0.5.0] - 2026-04-06

### Enhancement

- Review and tighten default auth_level (#97) 

### ⚙️ Miscellaneous Tasks

- Prepare v0.5.0 release — complete CHANGELOG (#118) 

### 🐛 Bug Fixes

- Resolve MkDocs strict-mode failures for nav, anchors, and links (#116) 
- Suppress bandit B311 false positive for non-security random usage (#88) 
- Apply Oracle PR review — deep immutability, regression test, docs sync 
- Switch Mermaid fence format to fence_div_format for rendering 

### 📚 Documentation

- Rewrite deployment guide for developer-friendly Azure Functions experience 
- Update README translations for OpenAPI removal (#99) 
- Update usage and deployment docs for OpenAPI removal (#99) 
- Update DESIGN.md and architecture docs for OpenAPI removal (#99) 
- Update CHANGELOG with breaking change for deprecated OpenAPI removal (#99) 
- Add Azure deployment verification note to README (#111) 
- Add Azure-verified sample output and update upgrade notes (#110) 
- Add comprehensive deployment guide with Azure provisioning and endpoint verification (#73) (#108) 
- Add production hardening guide (#105) 
- Add concurrency constraints, scale envelopes, SSE clarification, and non-goals (#90, #92, #93, #98, #100) (#103) 
- Add SDK compatibility policy and contract tests (#91) (#102) 
- Apply Oracle review fixes for PR #85 
- Restructure README and DESIGN.md for ecosystem positioning 
- Pin Mermaid JS version and add site_url 
- Fix DESIGN.md title and architecture factual accuracy (#77) 
- Fix architecture doc inaccuracies from Oracle post-merge review 
- Standardize architecture docs with Mermaid diagrams, Sources, and See Also 

### 🚀 Features

- Add CloneableGraph protocol and refactor _get_threadless_graph (#95, #96) 
- Add openapi bridge module for ecosystem integration 
- Add metadata dataclasses with immutable snapshot API 

### 🚜 Refactor

- Remove deprecated OpenAPI endpoint and _build_openapi() method (#99) 
- Split platform/routes.py into resource modules (#89) (#101) 

### 🧪 Testing

- Update existing tests for v0.5.0 compatibility 
## [0.4.0] - 2026-04-05

### 📚 Documentation

- Update documentation and release v0.4.0 (#62) 

### 🚀 Features

- Add Azure Table Storage ThreadStore (#59) (#69) 
- Add Azure Blob Storage checkpoint saver (#60) (#68) 
- Add thread state update and history endpoints (#57, #58) 
- Add threadless runs (POST /runs/wait, POST /runs/stream) (#53) (#66) 
- Add POST /threads/search and /threads/count endpoints (#55) (#65) 
- Add PATCH/DELETE /threads/{thread_id} endpoints (#54) (#64) 
- Add POST /assistants/count endpoint and name filter (#56) (#63) 

### 🧪 Testing

- Add persistent storage integration tests (#61) 
## [0.3.0] - 2026-04-04

### Release

- V0.3.0 — Platform API Compatibility Layer (#52) 

### 📚 Documentation

- Update all documentation for v0.2.0 release (#43) 
- Update README.md for v0.2.0 release (#30) 

### 🚀 Features

- Input validation and request size limits (#40) (#49) 
- Platform-compatible SSE streaming format (#39) (#48) 
- LangGraph Platform API compat route layer (#38) (#47) 
- Add ThreadStore protocol and InMemoryThreadStore (#46) 
- Add Platform API Pydantic contracts (#36) (#45) 

### 🚜 Refactor

- Extract handlers into _handlers.py and create platform/ subpackage (#44) 

### 🧪 Testing

- Langgraph_sdk compatibility tests (#42) (#51) 
- Integration tests with real LangGraph graphs (#41) (#50) 
## [0.2.0] - 2026-04-04

### Release

- V0.2.0 — Milestone 1 complete (#29) 

### ⚙️ Miscellaneous Tasks

- Add release automation workflow (#27) 
- *(deps)* Bump mypy from 1.19.1 to 1.20.0 (#1) 
- *(deps)* Update pytest-asyncio requirement (#2) 
- *(deps)* Bump github/codeql-action from 4.34.1 to 4.35.1 (#3) 
- *(deps)* Bump ruff from 0.15.7 to 0.15.8 (#4) 

### 🐛 Bug Fixes

- Sanitize graph failures and refine OpenAPI graph paths (#16) 
- Add TYPE_CHECKING import so mkdocstrings can discover LangGraphApp (#14) 
- Warn when anonymous auth is used in production (#13) 
- Return 501 for stream requests on invoke-only graphs (#12) 
- Add bounded buffering for stream responses (#11) 
- Align OpenAPI paths with registered route templates (#10) 

### 🚀 Features

- Export StateResponse and StatefulGraph in public API (#28) 
- Re-export contracts and protocols from package root (#26) 
- Add state endpoint for thread state retrieval (#24) 
- Add per-graph auth_level override (#23) 
- Add standalone deployable example with Oracle review fixes 
- Initial release of azure-functions-langgraph 0.1.0a0 

### 🧪 Testing

- Raise coverage to 98% with 102 tests, set fail_under=90 (#25) 
<!-- generated by git-cliff -->
