# Contributing to Azure Functions LangGraph

Thank you for your interest in contributing!

## Development Setup

```bash
git clone https://github.com/yeongseon/azure-functions-langgraph.git
cd azure-functions-langgraph
make install
```

## Available Commands

```bash
make test       # Run tests
make lint       # Run linter + type checker
make format     # Auto-format code
make cov        # Run tests with coverage report
make build      # Build distribution package
make security   # Run Bandit security scanner
make check-all  # Run all checks (lint + test + security)
```

## Code Style

- **Formatter**: Ruff
- **Linter**: Ruff (E, F, I rules)
- **Type Checker**: mypy (strict mode)
- **Line Length**: 100 characters

## Testing

- All new features must include tests
- Maintain ≥ 80% code coverage
- Use pytest with pytest-asyncio for async tests

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with tests
3. Run `make check-all` to verify everything passes
4. Submit a PR with a clear description

## Commit Messages

Follow conventional commits:

```
feat: add new feature
fix: fix a bug
docs: update documentation
test: add or update tests
build: update build configuration
```

## Release Process

Releases are automated via GitHub Actions when a version tag is pushed:

```bash
make release VERSION=x.y.z
```

## Documentation

- Keep `README.md` synchronized with code changes
- Update `PRD.md` when product requirements change
- Update `DESIGN.md` when design decisions change

## Coverage Policy

- Target: ≥ 80% line coverage
- New modules must meet the coverage floor
- Coverage is enforced in CI via `fail_under = 80`
