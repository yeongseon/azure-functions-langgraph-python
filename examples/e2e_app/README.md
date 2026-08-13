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
   `platform_compat`, so no Blob/Table storage is required — the graph name is
   `e2e_agent`.

To reproduce locally you must first place a built wheel in `wheels/` and add it
to `requirements.txt`, then `func start`.
