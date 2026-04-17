# Contributing Guidelines

Thank you for your interest in contributing to `azure-functions-langgraph-python`.

For complete contributing guidelines, please see the [CONTRIBUTING.md](https://github.com/yeongseon/azure-functions-langgraph-python/blob/main/CONTRIBUTING.md) file in the repository root.

## Quick reference

### Setup

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph-python.git
cd azure-functions-langgraph-python
make install
```

### Before submitting a PR

```bash
make check-all
```

This runs:

- `ruff check` — linting
- `mypy` — type checking
- `pytest` — test suite
- `bandit` — security scan

### PR checklist

- [ ] Tests added for new functionality
- [ ] All existing tests pass (`make test`)
- [ ] Linting passes (`make lint`)
- [ ] Type checking passes (`make typecheck`)
- [ ] Documentation updated if public API changed
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)

### Commit message format

```
type(scope): description

Examples:
feat(app): add support for multiple stream modes
fix(contracts): handle empty config gracefully
docs: update quickstart example
test: add coverage for invoke-only graphs
```

### Development workflow

See [Development](development.md) for detailed setup and workflow instructions.
