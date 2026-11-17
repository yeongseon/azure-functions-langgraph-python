# First-run baseline

Tracker #913 asks for a measured before/after of the first-run path. This page
records that measurement so a later change can be shown as a delta. **Baseline**
is frozen at commit `164993d` on 2026-11-17. **Current** is recomputed from the
repository by `tests/test_onboarding_baseline.py`, which fails when the column is
stale. The path measured is the [5-minute tutorial](tutorial-5-min.md) and
`examples/simple_agent/`.

## What is measured

| Measure | Baseline | Current | Computed from |
| --- | --- | --- | --- |
| Integration code lines | 9 | 9 | `examples/simple_agent/function_app.py` |
| Integration top-level statements | 5 | 5 | `examples/simple_agent/function_app.py` |
| Constructor arguments passed | 0 | 0 | `LangGraphApp(...)` call in `function_app.py` |
| Minimal app files | 5 | 5 | `examples/simple_agent/` |
| Minimal app requirements | 4 | 4 | `examples/simple_agent/requirements.txt` |
| Command lines to the first local response | 6 | 6 | `docs/tutorial-5-min.md`, Step 1 to the first `curl` |
| Command lines to deploy and verify | 13 | 13 | `docs/tutorial-5-min.md`, Step 5 |
| Shell variable assignments among them | 5 | 5 | `docs/tutorial-5-min.md`, Step 5 |
| Public LangGraphApp parameters (snapshot) | 17 | see [API inventory](api-inventory.md) | `LangGraphApp.__init__` (baseline only) |
| Package exported names (snapshot) | 31 | see [API inventory](api-inventory.md) | `azure_functions_langgraph.__all__` (baseline only) |

The two snapshot rows were measured on `main` at `164993d` on 2026-11-17 by
introspecting the source tree (`PYTHONPATH=src`): the parameters of
`LangGraphApp.__init__` without underscore-prefixed internals (19 in the
signature, minus `_registrations` and `_sb_registrations`; all defaulted), and
`len(azure_functions_langgraph.__all__)`. They are not recomputed here; public
API changes are tracked by the [API inventory](api-inventory.md).

## How the current values are computed

`tests/test_onboarding_baseline.py` reads the files and applies these rules:

- **Integration code lines:** non-blank, non-comment lines of `function_app.py`, module docstring excluded.
- **Top-level statements:** top-level statements of that file (`ast`), module docstring excluded.
- **Constructor arguments passed:** positional plus keyword arguments of the `LangGraphApp(...)` call.
- **Minimal app files:** files directly in `examples/simple_agent/`, excluding `README.md` and dotfiles.
- **Minimal app requirements:** non-blank, non-comment lines of `requirements.txt`.
- **Command lines to the first local response:** logical lines in the `bash` fences from `## Step 1` up to and including the first line starting with `curl`. Lines ending in a backslash are joined; blank and `#` lines are skipped.
- **Command lines to deploy and verify:** the same rule over every `bash` fence from `## Step 5` up to `## How this tutorial stays honest`.
- **Shell variable assignments:** those logical lines matching `^[A-Z_][A-Z0-9_]*=`.

## Friction observed

- The tutorial starts with a `git clone` of the whole repository, so the path does not show how to add the package to an existing project. That is a separate part of #913.
- With the tutorial's empty `AzureWebJobsStorage` (Core Tools 4.8.0, no storage emulator) the host logs `Process reporting unhealthy: Unhealthy` with `"azure.functions.webjobs.storage":{"status":"Unhealthy","description":"Unable to create client for AzureWebJobsStorage"}` after start, while the web host and script host lifecycle entries stay Healthy and requests keep being answered. The tutorial notes this in Step 2.
- First-run size: 9 code lines, 5 files, 4 requirements, 6 command lines to the first local response and 13 to deploy and verify (5 of them variable assignments).

## Time to first successful local response

A single set of runs on one machine, not a guarantee. Measured 2026-11-17 following the tutorial's steps in a fresh directory, with the example's own files from a fresh clone.

| Step | Cold package cache | Warm package cache |
| --- | --- | --- |
| `git clone` | 1.28 s | 1.15 s |
| `python3.11 -m venv .venv` | 3.46 s | 3.51 s |
| `pip install -r requirements.txt` | 10.32 s | 67.47 s |
| `func start` until `/api/health` returns 200, run 1 / 2 / 3 | 4.75 / 5.26 / 5.09 s | 2.81 / 10.05 / 14.52 s |
| Median `func start` to health 200 | 5.09 s | 10.05 s |
| First documented invoke request, run 1 / 2 / 3 | 0.037 / 0.038 / 0.007 s | 0.032 / 0.067 / 0.077 s |

Environment: Ubuntu 22.04.5, Linux 5.15.0-203-generic, 6 CPUs (Intel Core i5-9400F),
Python 3.11.15, pip 24.0, Azure Functions Core Tools 4.8.0,
`azure-functions-langgraph` 0.12.2 from PyPI. "Cold" used `pip install --no-cache-dir`;
"warm" used the default pip cache, whose prior contents were not controlled.
The 1-minute load average was about 20 at the start of both sets (it did not drop
below 3.0 within the polling window) and 25 before the warm host runs, so these
numbers are inflated and noisy. The warm install being slower than the cold one
shows how much the load distorts them; its cause was not investigated, so do not
compare the cold and warm columns.

Prerequisites (not measured and not included above): Python 3.11+,
Azure Functions Core Tools v4, `curl`, and network access to GitHub and PyPI.

## Updating this page

When the example, the tutorial or the constructor changes on purpose, update the
**Current** column in the same pull request; the test fails otherwise. Never edit
the **Baseline** column: the test pins it.
