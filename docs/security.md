# Security

## Authentication

By default, `LangGraphApp` creates endpoints with `AuthLevel.ANONYMOUS`. For production deployments, consider setting a higher auth level:

```python
import azure.functions as func

from azure_functions_langgraph import LangGraphApp

app = LangGraphApp(auth_level=func.AuthLevel.FUNCTION)
```

See [Azure Functions authentication](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-http-webhook-trigger#authorization-keys) for details on function keys and admin keys.

## Input validation

All request bodies are validated using Pydantic v2 models before being passed to the graph. Invalid requests receive a 422 response with validation error details.

## Secret management

This library does not handle secrets (API keys, connection strings, etc.). Use Azure Functions application settings or Azure Key Vault for secret management:

```python
import os

api_key = os.environ["OPENAI_API_KEY"]
```

See [Azure Functions app settings](https://learn.microsoft.com/en-us/azure/azure-functions/functions-how-to-use-azure-function-app-settings) for configuration guidance.

## Dependency security

The project uses:

- **Bandit** for static security analysis of Python code
- **Dependabot** for automated dependency updates
- **CodeQL** for code scanning via GitHub Actions

Run the security scan locally:

```bash
make security
```

## Reporting vulnerabilities

If you discover a security vulnerability, please report it responsibly. See [SECURITY.md](https://github.com/yeongseon/azure-functions-langgraph/blob/main/SECURITY.md) for reporting instructions.

Do not open a public GitHub issue for security vulnerabilities.
