# Installation

## Requirements

- Python 3.10 or later
- Azure Functions Core Tools (for local development)
- An Azure Functions project using the [Python v2 programming model](https://learn.microsoft.com/en-us/azure/azure-functions/functions-reference-python)

## Install from PyPI

```bash
pip install azure-functions-langgraph-python
```

This installs the package along with its dependencies:

- `azure-functions` — Azure Functions Python SDK
- `langgraph` (>= 0.2) — LangGraph graph runtime
- `pydantic` (>= 2.0, < 3.0) — request/response validation

## Add to your requirements

In your Azure Functions project, add to `requirements.txt`:

```text
azure-functions
langgraph
azure-functions-langgraph-python
```

Or if using `pyproject.toml`:

```toml
dependencies = [
    "azure-functions",
    "langgraph",
    "azure-functions-langgraph-python",
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
# 0.1.0a0
```

```python
from azure_functions_langgraph import LangGraphApp

app = LangGraphApp()
print(app)
# LangGraphApp(auth_level=<AuthLevel.ANONYMOUS: 'anonymous'>)
```
