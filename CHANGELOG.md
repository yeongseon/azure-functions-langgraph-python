# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### ✨ Features

- *(stores)* Add `reset_stale_locks()` to `AzureTableThreadStore` for recovering orphaned run locks (#157)
- *(docs)* Document best-effort lock release and run lock semantics in README
- *(examples)* Add `maintenance_timer` example for periodic stale lock recovery

### 🐛 Bug Fixes

- *(packaging)* Rename PyPI distribution back to azure-functions-langgraph 

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

### ⚙️ Miscellaneous Tasks

- *(deps)* Bump softprops/action-gh-release from 2 to 3 
- *(deps)* Bump mypy from 1.20.0 to 1.20.1 
- *(deps)* Bump actions/upload-artifact from 7.0.0 to 7.0.1 
- *(deps)* Bump github/codeql-action from 4.35.1 to 4.35.2 
- *(deps)* Bump actions/github-script from 8.0.0 to 9.0.0 
- Update repo references for azure-functions-{feature}-python naming convention 

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
