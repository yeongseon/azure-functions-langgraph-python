# Conversation Memory Example

**Continue here after the [Azure OpenAI agent](../azure_openai_agent/).** That
example is intentionally **stateless** — every request starts fresh. Real
assistants need to remember the conversation, so this example adds exactly one
thing: a **checkpointer**. Passing the same `thread_id` across HTTP requests now
keeps the conversation going; a different `thread_id` is an independent
conversation.

The graph shape is otherwise identical to `azure_openai_agent` (a real Azure
OpenAI chat agent with a deterministic fake-model fallback for
credential-free CI). The **only** delta is:

```python
# graph.py — the stateful change is the checkpointer
compiled_graph = builder.compile(checkpointer=InMemorySaver())
```

## Concepts (kept distinct on purpose)

| Term | What it is | Used here? |
| --- | --- | --- |
| **`thread_id`** | LangGraph conversation identity, sent per request under `config.configurable.thread_id`. Same id → same conversation. | **Yes** — the core of this example. |
| **Checkpointer** | Persists a thread's graph execution state between invokes. This example uses `InMemorySaver`. | **Yes** — this is the stateful delta. |
| **Platform `ThreadStore`** | Metadata registry for the optional `platform_compat` layer. **Not** the same as conversation memory. | No — not required for native memory. |
| **LangGraph `BaseStore`** | Long-term, cross-thread memory. | No — out of scope. |

> **Thread metadata storage is *not* conversation memory.** The checkpointer is
> what remembers the conversation. The Platform `ThreadStore` only tracks thread
> metadata for the SDK-compatibility layer. See `PRD.md` / `COMPATIBILITY.md`.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+
- *(Optional)* an **Azure OpenAI** resource with a chat deployment. Without one,
  the example runs against a deterministic fake model — perfect for trying the
  memory behaviour locally with zero credentials.

## Run locally

```bash
cd examples/conversation_memory
cp local.settings.json.example local.settings.json
pip install -r requirements.txt
func start
```

To use a real Azure OpenAI deployment, fill in the `AZURE_OPENAI_*` values in
`local.settings.json` (same variables and auth paths as the
[`azure_openai_agent`](../azure_openai_agent/#configuration) example).

## Walk through one conversation

The native invoke endpoint is the primary teaching path. The conversation
identity lives under `config.configurable.thread_id`.

**Request 1 — introduce yourself on thread `alice`:**

```bash
curl -s -X POST http://localhost:7071/api/graphs/conversation_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{
        "input": {"messages": [{"role": "human", "content": "My name is Alice."}]},
        "config": {"configurable": {"thread_id": "alice"}}
      }'
```

**Request 2 — ask on the *same* thread `alice`:**

```bash
curl -s -X POST http://localhost:7071/api/graphs/conversation_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{
        "input": {"messages": [{"role": "human", "content": "What is my name?"}]},
        "config": {"configurable": {"thread_id": "alice"}}
      }'
```

The second response's `messages` include the **earlier** turn (`"My name is
Alice."`) because the checkpointer replayed thread `alice`'s state before
running the graph. With a real Azure OpenAI deployment the assistant answers
`"Alice"` from that history; with the fake model you still see the accumulated
history that makes the answer possible.

**Request 3 — a *different* thread `bob` is independent:**

```bash
curl -s -X POST http://localhost:7071/api/graphs/conversation_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{
        "input": {"messages": [{"role": "human", "content": "What is my name?"}]},
        "config": {"configurable": {"thread_id": "bob"}}
      }'
```

Thread `bob` has no prior turns, so it never sees Alice's messages — conversations
are isolated per `thread_id`.

## Inspect thread state

The native state endpoint returns the persisted state for a thread:

```bash
curl -s http://localhost:7071/api/graphs/conversation_agent/threads/alice/state
```

```json
{
  "values": {
    "messages": [
      {"content": "My name is Alice.", "type": "human", "...": "..."},
      {"content": "...", "type": "ai", "...": "..."},
      {"content": "What is my name?", "type": "human", "...": "..."},
      {"content": "...", "type": "ai", "...": "..."}
    ]
  },
  "next": [],
  "metadata": {"...": "..."}
}
```

`values` is the checkpointed graph state; each `messages` item is a **serialized
LangChain message** (`content`, `type`, `additional_kwargs`, ...), not a bare
`{"role", "content"}` pair. Asking for a never-seen thread returns `404`.

## What happens after a host restart?

This example uses **`InMemorySaver`**, so thread state lives only in the running
Functions **host process**. Restart `func start` (or let Azure recycle/scale the
instance) and every thread's memory is gone — and because it is per-process, it
is **not** shared across multiple Function App instances either.

That trade keeps onboarding dependency-free while the concept is what matters.
For restart-safe and scale-safe memory, keep the exact same graph and swap the
checkpointer for a durable backend:

- **Azure Blob + Table** → [`persistent_agent_blob_table`](../persistent_agent_blob_table/)
- **Managed Identity (no secrets in App Settings)** → [`managed_identity_storage`](../managed_identity_storage/)
- **SQLite (single-instance local/dev)** → [`sqlite_checkpoint_local`](../sqlite_checkpoint_local/)
- **Postgres (multi-instance prod)** → [`postgres_checkpoint_production`](../postgres_checkpoint_production/)

## Concurrency

Native invoke/stream endpoints take an **in-process per-thread lock** when the
graph has a checkpointer and the request carries a `thread_id`, so concurrent
requests on the **same** thread within one worker are serialized. This lock is
**in-process only** — multi-instance deployments must use a distributed
`ThreadLock` backend or Platform-compatible runs with `AzureTableThreadStore`.
See the main README's *Distributed thread locking* section.

## Deploy to Azure

Application code does not change between local and Azure. Follow the canonical
[deployment guide](../../docs/deployment.md).

> **Production auth:** `LangGraphApp` defaults to `AuthLevel.FUNCTION`, so the
> deployed endpoints require a function key (`?code=<FUNCTION_KEY>` or the
> `x-functions-key` header).

> **Memory backend for production:** `InMemorySaver` is **not** production-safe
> (state is lost on restart and never shared across instances). Switch to a
> durable checkpointer — see the links above — before deploying anything that
> must remember conversations.

## CI / credential-free runs

Unless Azure OpenAI is **fully** configured — endpoint, deployment, **and** an
auth method (`AZURE_OPENAI_API_KEY`, or `AZURE_OPENAI_USE_ENTRA_ID=true`) —
`graph.py` builds a deterministic `GenericFakeChatModel` instead of calling Azure
OpenAI. This keeps the example importable and smoke-testable in CI without any
cloud credentials, and the fake path never imports `langchain_openai`.
