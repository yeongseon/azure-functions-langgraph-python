# Installation

## Requirements

- Python 3.11–3.14 (`>=3.11,<3.15`). Azure Functions has GA support for Python
  3.11, 3.12, 3.13, and 3.14; see
  [Azure Functions supported languages](https://learn.microsoft.com/en-us/azure/azure-functions/supported-languages)
  for the current support window.
- Azure Functions Core Tools (for local development)
- An Azure Functions project using the [Python v2 programming model](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## Install from PyPI

```bash
pip install azure-functions-langgraph
```

This installs the package along with its dependencies:

- `azure-functions` — Azure Functions Python SDK
- `langgraph` (>= 1.0, < 2.0) — LangGraph graph runtime
- `pydantic` (>= 2.7.4, < 3.0) — request/response validation

The authoritative constraints live in the package metadata (`pyproject.toml` /
`pip show azure-functions-langgraph`); the list above is a summary.

## Add to your requirements

In your Azure Functions project, add to `requirements.txt`:

```text
azure-functions
langgraph>=1.0,<2.0
azure-functions-langgraph>=0.12.0,<0.13
```

Pin to the minor range you tested against. Only versions that PyPI lists can be installed: a few
tagged versions never reached PyPI (see [Release status](release-status.md)), so an exact `==` pin to
one of them fails, while a range such as the one above is not affected. The default-path example
(`examples/simple_agent`, used by the [5-minute tutorial](tutorial-5-min.md)) and the four
primary learning-path examples use the same `>=0.12.0,<0.13` form. CI certifies only the
primary examples against the published release (see
[Dependency target and clean-install certification](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/examples/README.md#dependency-target-and-clean-install-certification));
other examples may still target an older minor line.

Or if using `pyproject.toml`:

```toml
dependencies = [
    "azure-functions",
    "langgraph>=1.0,<2.0",
    "azure-functions-langgraph>=0.12.0,<0.13",
]
```

## Development installation

Clone the repository and install with development dependencies:

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python
make install
```

This creates a virtual environment, installs Hatch, and sets up the development environment with all tools (ruff, mypy, pytest, pre-commit).

## Verify installation

```python
import azure_functions_langgraph

print(azure_functions_langgraph.__version__)
# X.Y.Z — e.g. 0.10.0
```

```python
from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()
print(app)
# LangGraphApp(auth_level=<AuthLevel.FUNCTION: 'function'>, health_auth_level=<AuthLevel.ANONYMOUS: 'anonymous'>, ...)
```
