# Development

## Setup

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph
make install
```

This will:

1. Create a Python virtual environment in `.venv/`
2. Install Hatch as the build and environment manager
3. Create the Hatch environment with all dev dependencies
4. Install pre-commit hooks

## Project structure

```
azure-functions-langgraph/
├── src/azure_functions_langgraph/   # Source code
│   ├── __init__.py                  # Package init, version, lazy import
│   ├── app.py                       # LangGraphApp class
│   ├── contracts.py                 # Pydantic models
│   ├── protocols.py                 # Protocol interfaces
│   └── py.typed                     # PEP 561 typed marker
├── tests/                           # Test suite
│   ├── conftest.py                  # Shared fixtures (FakeCompiledGraph, etc.)
│   ├── test_app.py                  # Core app tests
│   ├── test_contracts.py            # Contract model tests
│   ├── test_protocols.py            # Protocol tests
│   └── test_public_api.py           # Public API surface tests
├── examples/                        # Example applications
│   └── simple_agent/
│       └── graph.py
├── docs/                            # Documentation (MkDocs)
├── pyproject.toml                   # Project metadata and tool config
├── Makefile                         # Development commands
└── .pre-commit-config.yaml          # Pre-commit hooks
```

## Makefile commands

| Command | Description |
|---------|-------------|
| `make install` | Create environment and install dependencies |
| `make test` | Run pytest test suite |
| `make lint` | Run ruff check + mypy |
| `make format` | Auto-format code with ruff |
| `make typecheck` | Run mypy type checker |
| `make security` | Run bandit security scan |
| `make build` | Build sdist and wheel |
| `make cov` | Run tests with HTML coverage report |
| `make docs` | Build MkDocs documentation |
| `make docs-serve` | Serve docs locally |
| `make check` | Run lint + typecheck |
| `make check-all` | Run lint + typecheck + test + security |
| `make clean` | Remove build artifacts |
| `make clean-all` | Remove all generated files including venv |

## Code style

- **Formatter**: ruff format (line length 100)
- **Linter**: ruff check (E, F, I rules)
- **Type checker**: mypy (strict mode)
- **Target**: Python 3.10+

## Adding a new feature

1. Write tests first in `tests/`
2. Implement in `src/azure_functions_langgraph/`
3. Run `make check-all` to verify everything passes
4. Update documentation if the public API changed

## Pre-commit hooks

Pre-commit hooks run automatically on `git commit`:

- ruff format check
- ruff lint check
- trailing whitespace removal
- end-of-file fixer
- YAML/TOML syntax check
- mypy type check
- bandit security scan

To run manually:

```bash
make precommit
```
