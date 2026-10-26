# Deferred runtime edge triage

Assessment of #701 against the post-#746 adapter. These are repository-local
observations, not claims of production incidents or real-Azure certification.

| Candidate | Disposition and evidence |
| --- | --- |
| Deep JSON decoder recursion | A large literal body was already rejected on this interpreter. Injecting `HttpRequest.get_json` raising `RecursionError` reproduced an uncaught failure on all four native paths. Catch at the decoder boundary; return sanitized 400 with no policy/lock/graph calls. |
| Immediate synchronous `astream()` construction exception | Confirmed by code: construction follows `on_run_started` but precedes the iteration error boundary. Needs a focused lifecycle regression/fix; not claimed solved by the parsing fix. |
| `version` through `**kwargs` | Preserve legitimate wrappers; existing min/latest LangGraph lanes and version tests remain the evidence. No rejection based on signature shape alone. |
| True-stream `aclose()` cancellation | Current nested `finally` releases ownership even on `BaseException`; retain #732 terminal/cancellation tests, do not reimplement #656. |
| Response constructed but never iterated | Current code acquires before handing an unstarted generator to `StreamingResponse`; an unstarted generator cannot run its `finally`. Needs transport-lifetime reproduction and handoff design; potential leak, not yet a proven Azure incident. |
| Durable host/task cancellation | `except BaseException` currently normalizes cancellation. Requires dedicated cancellation characterization and propagation fix; serialization regression guarantees must remain. |
| Platform renewal worker exception | Fixed by #729; existing renewal-failure matrix checks failure reporting, cleanup and owner fencing. |
| Cosmos emulator replaced client | Replacement path still needs original-client lifecycle coverage; no production credential/TLS change is implied. |
| Blob hung renewal fairness | Existing bounded worker/renewal design was retained by #733 after #387. No distinct reproduction here justifies removing renewal or reopening the same issue. |

Unresolved lifecycle candidates remain on #701. They must receive deterministic
reproductions and independent focused changes before this tracker can close.
