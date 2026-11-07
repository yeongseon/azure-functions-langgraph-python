# Example guide

Choose a checked-in example by the capability you want to learn. Each link opens
that example's local README, which owns its exact dependencies, settings and run
commands. These are separate apps, not steps that accumulate into one app.

## Suggested progression

1. Read [Simple Agent](simple_agent.md), then run the deterministic greeting example.
2. Try the tool-calling example with `AGENT_MODEL_MODE=fake`. It performs a scripted
   tool loop without Azure OpenAI credentials; it does not prove real-model behavior.
3. Add state with the local SQLite example. Move to the storage examples only when
   their database, Azurite or Azure credentials are configured.
4. Compare native streaming with the true HTTP streaming example. Buffered/native
   streaming and incremental HTTP delivery are different contracts.

**Local-only graph logic is not a running Functions host.** HTTP examples still
need Core Tools and their README settings. Azurite emulates supported storage;
it is not proof of Azure deployment or multi-instance correctness. Never infer
cloud certification from a fake-model run, an import check or a local emulator.
See [verification levels](../testing.md#example-verification-levels) and the
[deployment guide](../deployment.md).

## Native graph basics

| Example and local README | Requirements and purpose |
| --- | --- |
| [simple_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/simple_agent/README.md) | Start with the greeting graph; no model credentials. |
| [official_langgraph_basic](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/official_langgraph_basic/README.md) | Official LangGraph workflow adapted to Functions. |
| [async_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/async_agent/README.md) | Async nodes and native async invocation. |
| [openapi_bridge](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/openapi_bridge/README.md) | OpenAPI wiring in isolation. |
| [production_auth](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/production_auth/README.md) | Compare public routes and function-key-protected routes. |
| [versioned_output_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/versioned_output_agent/README.md) | Opt-in v2 output shapes. |

## Models, tools and conversation memory

| Example and local README | Requirements and purpose |
| --- | --- |
| [azure_openai_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/azure_openai_agent/README.md) | Real Azure OpenAI configuration is required for model calls. |
| [conversation_memory](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/conversation_memory/README.md) | In-memory per-thread conversation; real-model settings follow the README. |
| [tool_calling_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/tool_calling_agent/README.md) | Use AGENT_MODEL_MODE=fake for a credential-free, scripted tool loop; real mode needs Azure OpenAI settings. |
| [production_persistent_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/production_persistent_agent/README.md) | Production agent with Azure Blob persistence; requires model and storage configuration. |

## Checkpoint storage

| Example and local README | Requirements and purpose |
| --- | --- |
| [sqlite_checkpoint_local](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/sqlite_checkpoint_local/README.md) | Local file, no external database. Single-instance development only, not multi-instance production. |
| [persistent_agent_blob_table](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/persistent_agent_blob_table/README.md) | Azure Blob checkpoints and Table thread store; use configured Azure storage or the documented Azurite setup. |
| [postgres_checkpoint_production](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/postgres_checkpoint_production/README.md) | A reachable Postgres database is required. |
| [cosmos_checkpoint_azure](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/cosmos_checkpoint_azure/README.md) | Azure Cosmos DB configuration and credentials are required. |
| [managed_identity_storage](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/managed_identity_storage/README.md) | Azure Managed Identity in production; follow the README for its local Azurite fallback. |

## Transports and run lifecycles

| Example and local README | Requirements and purpose |
| --- | --- |
| [platform_compat_sdk](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/platform_compat_sdk/README.md) | Platform-compatible routes and SDK client; needs a running Functions host and its documented settings. |
| [true_streaming_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/true_streaming_agent/README.md) | Incremental SSE over the opt-in FastAPI transport; needs streaming extras and a compatible Functions host. |
| [durable_async_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/durable_async_agent/README.md) | Experimental Durable lifecycle; needs the durable extra and Azure storage or Azurite. Durable history does not replace graph checkpoints. |
| [service_bus_agent](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/service_bus_agent/README.md) | Queue-triggered invocation; requires the documented Service Bus connection. |

## Observability

| Example and local README | Requirements and purpose |
| --- | --- |
| [run_observer](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/run_observer/README.md) | Deterministic graph and lifecycle logging; no model credentials. |
| [run_observer_otel](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/run_observer_otel/README.md) | Observer integration with optional OpenTelemetry dependencies. |
| [observability_app_insights](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/observability_app_insights/README.md) | Application Insights export requires its connection settings; see the README for KQL examples. |

## Operations and verification

| Example and local README | Requirements and purpose |
| --- | --- |
| [maintenance_timer](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/maintenance_timer/README.md) | Stale-lock maintenance for the Azure Table thread store; requires the matching storage configuration. |
| [local_curl](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/local_curl/README.md) | Client scripts against an already-running local Functions host. |
| [e2e_app](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/e2e_app/README.md) | Release/cloud test fixture, not a standalone credential-free deployment certification. |

## Compatibility boundaries

- The four primary learning-path examples and `simple_agent` target their
  checked-in published dependency bounds. This index does not retarget them to
  development `main`; see the [example inventory](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/README.md).
- True streaming needs its optional transport dependencies and supported host
  runtime. Follow its README's runtime and deployment-plan limitations.
- Durable async runs remain experimental. Storage, cancellation and deployment
  constraints still apply; polling a run is not graph checkpoint persistence.
- A `thread_id` separates state; it does not by itself establish authenticated
  user ownership. Follow each example's authentication boundary.

Check these checked-in links with `make docs-examples-check` before submitting
navigation changes. The check resolves repository links locally, without Azure
credentials or network requests.
