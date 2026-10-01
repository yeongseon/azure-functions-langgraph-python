# Simple Agent Example

Minimal LangGraph agent deployed as Azure Functions HTTP endpoints.
Two nodes (`greet` -> `farewell`) connected sequentially.

## Prerequisites

- [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4+
- Python 3.10+

## Run locally

```bash
cd examples/simple_agent
pip install -r requirements.txt
func start
```

## Test

```bash
# Invoke the agent
curl -X POST http://localhost:7071/api/graphs/simple_agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'

# Stream the agent response
curl -X POST http://localhost:7071/api/graphs/simple_agent/stream \
  -H "Content-Type: application/json" \
  -d '{"input": {"messages": [{"role": "human", "content": "World"}], "greeting": ""}}'

# Health check
curl http://localhost:7071/api/health
```
