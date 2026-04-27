# Examples

`azure-functions-langgraph` keeps a growing set of smoke-tested examples. Each example is a standalone Azure Functions app — see the README in each directory for local setup instructions.

| Role | Path | Description |
| --- | --- | --- |
| Minimal | [`simple_agent`](simple_agent/) | Two-node greeting agent with sequential edges. |
| Platform SDK | [`platform_compat_sdk`](platform_compat_sdk/) ⭐ | `platform_compat=True` host driven by the official `langgraph-sdk` client. |
| Persistent storage | [`persistent_agent_blob_table`](persistent_agent_blob_table/) | End-to-end Azure Blob checkpointer + Azure Table thread store, runnable on Azurite. |
| Managed Identity | [`managed_identity_storage`](managed_identity_storage/) | Same backends wired with `DefaultAzureCredential` for production, with Azurite fallback for local dev. |
| OpenAPI bridge | [`openapi_bridge`](openapi_bridge/) | Wires `register_with_openapi` into `azure-functions-openapi-python` for spec generation. |
| Per-graph auth | [`production_auth`](production_auth/) | Public health + anonymous demo graph alongside a function-key-protected graph. |
| Curl helpers | [`local_curl`](local_curl/) | Shell scripts for hitting every Quick Start endpoint locally. |

## Conventions

Every example ships with:

- `README.md` — copy-pasteable `func start` + verification curl
- `function_app.py` — the LangGraph wiring
- `graph.py` — the compiled graph
- `host.json` — the standard Azure Functions host config
- `local.settings.json.example` — copy to `local.settings.json` before running
- `requirements.txt` — pinned to a tested package version range

## Picking an example

- **Just want it running?** → `simple_agent`
- **Switching from LangGraph Cloud / using `langgraph-sdk`?** → `platform_compat_sdk`
- **Need state to survive restarts and scale-out?** → `persistent_agent_blob_table`
- **Deploying to Azure with Managed Identity (no secrets in App Settings)?** → `managed_identity_storage`
- **Want OpenAPI / Swagger UI for your endpoints?** → `openapi_bridge`
- **Mixing public and private graphs?** → `production_auth`
- **Verifying a deployed Function App from the terminal?** → `local_curl`
