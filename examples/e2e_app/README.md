# e2e_app — real-Azure certification app

This app exists **only** for the release gate. The `e2e-azure` GitHub workflow
deploys it to a temporary Azure Functions Consumption host, runs `tests/e2e`
against it, records an `azure-cert` artifact, then deletes the resource group.

It differs from the user-facing `examples/simple_agent` in two ways:

1. **Candidate under test.** `requirements.txt` does not pin
   `azure-functions-langgraph`. The workflow builds a wheel from the release
   commit, drops it in `wheels/`, and appends the local wheel path so the
   deployed host runs the exact source being certified (not the PyPI release).
2. **Native routes only.** `LangGraphApp()` is created without
   `platform_compat`. The baseline greeting graph `e2e_agent` has no
   checkpointer and needs only `AzureWebJobsStorage`.

It also registers a second graph, **`e2e_lock_agent`** (issue #386), used to
prove single-writer exclusivity across two Function Apps. Unlike `e2e_agent` it
*does* use Azure Blob storage: it is wired with an `AzureBlobLeaseThreadLock`
(distributed lease container) and an `AzureBlobCheckpointSaver` (single-writer
checkpoint container). Both containers live in the same storage account as
`AzureWebJobsStorage` and are pre-created by `infra/main.bicep`. The workflow
deploys this same payload to **two** Function Apps (App A and App B) that share
those containers; `tests/e2e/test_langgraph_lock_e2e.py` then drives a
concurrent same-`thread_id` invoke against both and asserts exactly one writer
wins (the loser gets HTTP 409). That two-app test skips unless both
`E2E_BASE_URL` and `E2E_BASE_URL_B` are set, so the single-app certification
path is unaffected.

Relevant app settings (identical on both hosts): `E2E_LOCK_CONTAINER`,
`E2E_CHECKPOINT_CONTAINER`, and optional `E2E_LOCK_HOLD_SECONDS` (default 20).

To reproduce locally you must first place a built wheel in `wheels/` and add it
to `requirements.txt`, then `func start`.
