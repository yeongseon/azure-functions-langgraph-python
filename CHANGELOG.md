# Changelog

All notable changes to this project will be documented in this file.
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

### 🚀 Features

- Add CloneableGraph protocol and refactor _get_threadless_graph (#95, #96) 
- Add openapi bridge module for ecosystem integration 
- Add metadata dataclasses with immutable snapshot API 

### 🚜 Refactor

- Remove deprecated OpenAPI endpoint and _build_openapi() method (#99) 
- Split platform/routes.py into resource modules (#89) (#101) 

### 🧪 Testing

- Update existing tests for v0.5.0 compatibility 
<!-- generated by git-cliff -->
