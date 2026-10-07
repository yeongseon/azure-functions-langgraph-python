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
azure-functions-langgraph>=0.10.0,<0.11
```

Pin to the minor range you tested against. The examples in this repository use
the same `>=0.10.0,<0.11` form.

Or if using `pyproject.toml`:

```toml
dependencies = [
    "azure-functions",
    "langgraph>=1.0,<2.0",
    "azure-functions-langgraph>=0.10.0,<0.11",
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
