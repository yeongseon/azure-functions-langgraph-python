# Offline Run-Observer Proof Example

Proves, with no Azure host, credentials or network, that the run telemetry
emitted by the built-in `LoggingRunObserver` is **payload-free**: safe
correlation fields appear, a dummy user-message *canary* does not.

It complements [`run_observer`](../run_observer/) (how to write your own
observer) and [`observability_app_insights`](../observability_app_insights/)
(shipping the records to Application Insights) by giving contributors a fast,
deterministic way to verify privacy-sensitive observer output.

## What it does

`proof.py` registers a deterministic graph (`graph.py`, no LLM) on a
`LangGraphApp(observer=LoggingRunObserver(...))` and calls the real registered
`invoke` handler in-process three times with fixed fake `X-Request-ID`s and a
canary string as the user message:

| Run | Request ID | Observer events |
| --- | --- | --- |
| success | `fake-request-success` | `started` → `completed` |
| failure (node raises, message embeds the canary) | `fake-request-failure` | `started` → `failed` (`error_type=RuntimeError` only) |
| rejected (malformed body containing the canary) | `fake-request-rejected` | `rejected` (`reason=invalid_request`), no `started` |

It then asserts the canary reached the app (it is echoed in the success
response) yet appears in **no** observer log message or structured field.

## Run it

```bash
cd examples/run_observer_offline_proof
pip install -r requirements.txt        # or run from a repo checkout with PYTHONPATH=../../src
python proof.py
```

Verified from a repo checkout with `PYTHONPATH=../../src python proof.py`. The
human-readable lines are (each is followed by a JSON line with the full
structured `langgraph_run` fields, including `thread_id`, `duration_ms`,
`error_type` / `reason`):

```text
langgraph.run.started {'graph_name': 'run_observer_offline_proof', 'endpoint': 'invoke', 'run_id': 'fake-request-success', 'status': 'started'}
langgraph.run.completed {'graph_name': 'run_observer_offline_proof', 'endpoint': 'invoke', 'run_id': 'fake-request-success', 'status': 'completed'}
langgraph.run.started {'graph_name': 'run_observer_offline_proof', 'endpoint': 'invoke', 'run_id': 'fake-request-failure', 'status': 'started'}
langgraph.run.failed {'graph_name': 'run_observer_offline_proof', 'endpoint': 'invoke', 'run_id': 'fake-request-failure', 'status': 'failed', 'error_type': 'RuntimeError'}
langgraph.run.rejected {'graph_name': 'run_observer_offline_proof', 'endpoint': 'invoke', 'run_id': 'fake-request-rejected', 'status': 'rejected', 'reason': 'invalid_request'}

OK: 5 observer records, canary absent from all of them.
```

The smoke test that enforces this, including a mutation check proving it fails
for an observer that logs the exception message:

```bash
pytest tests/test_run_observer_offline_proof_example.py
```

## Observer fields vs. model payload logging

The observer receives a `RunContext` of correlation identifiers (graph name,
endpoint, run id, thread id, assistant/invocation id, stream mode, transport,
checkpointer / lock backend) and timings. For failures it records only the
exception **class name**. It never receives request input, model output,
`config`, headers, or secrets.

Non-goals / what this does **not** prove:

- It does not cover logging done outside the observer. In the failure run above,
  the host's own error handler logs the graph exception **with its traceback and
  message** on a different logger (`Graph ... invoke failed`), so the canary
  does appear there. Observer output is payload-free; your application and host
  logging configuration is separate and must be reviewed on its own.
- It does not cover payload logging you add in your graph nodes or a custom
  observer.
- It does not change event contracts or production telemetry, and it does not
  validate Application Insights ingestion, sampling or redaction.
- It is not real-Azure verification.

## Azure prerequisites

None for the proof itself (`python proof.py` and the pytest above).
`function_app.py` is the normal Azure Functions entry point for the same wiring
and, to run under `func start`, needs
[Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
v4+ and `cp local.settings.json.example local.settings.json` (Azurite or a
storage connection string for `AzureWebJobsStorage`).
