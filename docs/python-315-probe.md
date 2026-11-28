# Python 3.15 candidate installation evidence

Python 3.15 is **not supported** by this package. This record addresses the
installation investigation in [issue #1014](https://github.com/yeongseon/azure-functions-langgraph-python/issues/1014),
not an Azure Functions deployment or a new compatibility promise.

## Scope and interpreter

On 2026-11-28, the source at commit
`8f63a6c5277dcc6fb73836daabee7ec26c8c74b8` was probed on Linux x86_64,
glibc 2.35. `uv 0.11.7` obtained **CPython 3.15.0a8**, not a final release,
using `uv python install 3.15`. Its installation directory, cache and seeded
virtual environment were owned by the issue worktree. The existing shared
`python3.15` executable was not replaced or used. pip in the candidate venv
was 26.3. No real Azure operation was performed.

The earlier maintainer comment on the issue reports a different interpreter,
3.15.0 final, and diagnostic environments missing `orjson`. Those results
are historical, attributed evidence; they were not reproduced or counted as
passing runtime tests in this pre-release run.

## Observed boundaries

All dry runs below used the candidate venv's `python -m pip install`.
Successful dependency resolution means only **would install**, not installed,
imported, exercised or supported. Dependency ranges were copied from current
metadata; optional dependency probes did not bypass the adapter's own bound.

| Selection | Result | Boundary observed |
| --- | --- | --- |
| Source tree, `--dry-run --no-deps .` | Failed during metadata generation | The isolated build environment's `packaging` calls `builtins.sentinel`, absent in 3.15.0a8; Hatchling cannot finish metadata generation |
| Candidate wheel built by `make build` on Python 3.11, `--dry-run --no-deps` | Rejected | `requires a different Python: 3.15.0 not in '<3.15,>=3.11'`; pip's diagnostic omits the alpha suffix, but the interpreter was 3.15.0a8 |
| `langgraph>=1,<2` (resolved candidate 1.2.24) | Failed | `zstandard==0.25.0` source-build requirements hit the same `packaging`/`builtins.sentinel` error |
| `azure-functions-openapi>=0.30,<1` | Rejected | Available 0.30.0, 0.30.1 and 0.31.0 releases declare `>=3.11,<3.15` |
| `langgraph-sdk>=0.2.2,<0.5` (candidate 0.4.14) | Failed | Transitive `zstandard` build-requirements error, as above |
| `azure-storage-blob>=12,<13` | Dry run succeeded | Candidate 12.31.1; storage calls not run |
| `azure-data-tables>=12,<13` | Dry run succeeded | Candidate 12.7.0; storage calls not run |
| `azure-identity>=1.16,<2` | Dry run succeeded | Candidate 1.27.0; credentials not requested |
| `langgraph-checkpoint-postgres>=3,<4` | Failed | Candidate 3.2.1; transitive `zstandard` build-requirements error |
| `langgraph-checkpoint-sqlite>=3,<4` | Failed | Candidate 3.1.3; transitive `zstandard` build-requirements error |
| `langgraph-checkpoint-cosmosdb>=0.2,<0.5` | Failed | Candidate 0.4.0; transitive `zstandard` build-requirements error |
| `opentelemetry-api>=1.20,<2` | Dry run succeeded | Candidate 1.45.2; observer not exercised |
| `psycopg[binary]>=3,<4`, streaming and Durable dependencies together | Dry run succeeded | Candidates 3.3.9, `azurefunctions-extensions-http-fastapi` 1.0.1 and `azure-functions-durable` 1.7.0; no database, transport or orchestration runtime tested |
| Official basic example's unchanged `requirements.txt` | Rejected | Published `azure-functions-langgraph==0.14.2` excludes 3.15; upstream graph and HTTP handler were not run |
| Actual installation of `orjson==3.13.0` | Failed building wheel | pip selected the source distribution; installed Rust 1.94.0 cannot build a package requiring Rust 1.95 |
| Pydantic floor `==2.7.4`, `--dry-run --only-binary=:all:` | Rejected | No compatible wheel for its exact `pydantic-core==2.18.4`; this does not prove a source build is impossible |

Native invoke/stream/state, real OpenAPI interoperability, actual SDK calls,
adapter persistence and the official graph therefore remain **unverified on
3.15**. There was no dependency-complete adapter installation. No missing
dependency was stubbed out and no `Requires-Python` check was overridden.

## Reproduce the installation probes

Run in an owned checkout. Keep the absolute path printed by uv, not a shared
executable discovered through PATH. If uv cannot obtain 3.15, record that
failure instead of substituting another interpreter. A later download or
resolver may produce different versions and requires a new dated record.

```bash
export UV_PYTHON_INSTALL_DIR="$PWD/.candidate-python"
export UV_CACHE_DIR="$PWD/.uv-cache"
uv python install 3.15
# Path obtained in this run; substitute only the actual downloaded interpreter.
uv venv --seed --python \
  "$UV_PYTHON_INSTALL_DIR/cpython-3.15.0a8-linux-x86_64-gnu/bin/python3.15" .venv-315
.venv-315/bin/python --version
.venv-315/bin/python -m pip install --dry-run --no-deps .
.venv-315/bin/python -m pip install --dry-run 'langgraph>=1,<2'
.venv-315/bin/python -m pip install --dry-run 'azure-functions-openapi>=0.30,<1'
.venv-315/bin/python -m pip install --dry-run 'langgraph-sdk>=0.2.2,<0.5'
.venv-315/bin/python -m pip install --dry-run -r examples/official_langgraph_basic/requirements.txt
.venv-315/bin/python -m pip install 'orjson==3.13.0'
.venv-315/bin/python -m pip install --dry-run --only-binary=:all: 'pydantic==2.7.4'
```

The storage and other optional dependency probes use the same dry-run command
with each table entry's requirement. Build the candidate wheel using
`make build` in a separate owned Python 3.11 Hatch environment, then pass its
actual wheel path to the candidate command with `--dry-run --no-deps`.

## Metadata and CI disposition

Retain `Requires-Python = ">=3.11,<3.15"`, the Python 3.11–3.14 classifiers,
the Azure Functions interpreter-marker split and all dependency bounds.
These results do not justify a new classifier, a relaxed upper bound, a
higher Pydantic floor or a workaround that silently omits the OpenAPI extra.
The existing CI remains unchanged: the 3.11–3.14 suite matrix, the 95% coverage
gate on 3.11, the exact-floor lane (including `langsmith<0.15`), LangGraph
minimum/latest lanes and SDK legacy/maximum lanes preserve existing coverage.
Configured CI axes are not evidence that this local run exercised those axes.

An experimental 3.15 CI lane remains a maintainer decision, not an implemented
or authorized outcome of this record. Before expanding support, obtain a
dependency-complete candidate install on a named interpreter/image/toolchain;
run source and installed-wheel native invoke/stream/state, real OpenAPI and SDK
integration, persistence and official-example checks with failures and skips
accounted for. Choose bounds and maintained CI axes from that evidence.

Package installation and Azure host support are separate. Existing deployment
evidence stays scoped to the named runs in
[Compatibility Policy](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/COMPATIBILITY.md)
and the [official example](https://github.com/yeongseon/azure-functions-langgraph-python/tree/main/examples/official_langgraph_basic).
Nothing here adds a 3.15 deployment claim. Both issue acceptance checkboxes
remain incomplete pending runtime evidence and the maintainer's CI/support
decision.
