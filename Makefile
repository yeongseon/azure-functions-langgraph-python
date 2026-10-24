VENV_DIR := .venv
PYTHON := $(VENV_DIR)/bin/python
PIP := $(VENV_DIR)/bin/pip
HATCH := $(VENV_DIR)/bin/hatch

.PHONY: bootstrap
bootstrap:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv $(VENV_DIR); \
	fi
	@echo "Ensuring Hatch is installed in virtual environment..."
	@$(PIP) install --upgrade pip > /dev/null
	@$(PIP) install hatch > /dev/null
	@echo "Hatch installed at $(HATCH)"

.PHONY: ensure-hatch
ensure-hatch: bootstrap

.PHONY: install
install: ensure-hatch
	@$(HATCH) env create
	@if [ -n "$$CI" ]; then \
		echo "CI detected: skipping pre-commit hook installation"; \
	else \
		$(MAKE) precommit-install; \
	fi

.PHONY: shell
shell: ensure-hatch
	@$(HATCH) shell

.PHONY: reset
reset: clean-all install
	@echo "Project reset complete."

.PHONY: hatch-clean
hatch-clean: ensure-hatch
	@$(HATCH) env remove || echo "No hatch environment to remove"

.PHONY: format
format: ensure-hatch
	@$(HATCH) run format

.PHONY: format-check
format-check: ensure-hatch
	@$(HATCH) run format-check

.PHONY: style
style: ensure-hatch
	@$(HATCH) run style

.PHONY: typecheck
typecheck: ensure-hatch
	@$(HATCH) run typecheck

.PHONY: lint
lint: ensure-hatch
	@$(HATCH) run lint

.PHONY: security
security: ensure-hatch
	@$(HATCH) run security

.PHONY: check
check: ensure-hatch
	@$(MAKE) lint
	@$(MAKE) typecheck
	@echo "Lint and type check passed."

.PHONY: lint-workflows
lint-workflows: ensure-hatch
	@$(HATCH) run python tools/lint_release_workflows.py
	@$(HATCH) run python tools/lint_workflow_pins.py
	@$(HATCH) run python tools/lint_hatch_matrix.py

.PHONY: check-all
check-all: ensure-hatch
	@$(MAKE) lint-workflows
	@$(MAKE) check
	@$(MAKE) test
	@$(MAKE) security
	@echo "All checks passed including tests and security scan."

.PHONY: precommit
precommit: ensure-hatch
	@$(HATCH) run precommit

.PHONY: precommit-install
precommit-install: ensure-hatch
	@$(HATCH) run precommit-install

.PHONY: test
test: ensure-hatch
	@echo "Running tests..."
	@$(HATCH) run test

.PHONY: certify-examples-published
certify-examples-published: ensure-hatch
	@$(HATCH) run python scripts/certify_primary_examples.py published

.PHONY: certify-examples-candidate
certify-examples-candidate: ensure-hatch
	@test -n "$(WHEEL)" || (echo "WHEEL=/path/to/candidate.whl is required" && exit 2)
	@$(HATCH) run python scripts/certify_primary_examples.py candidate --wheel "$(WHEEL)"

# Emulator lanes: each run starts its own Compose project (lg-<lane>-<shell pid>) with the emulator
# published on 127.0.0.1 only, and removes that project on success, failure or interrupt. A run
# never stops or removes a project it did not start. The INT/TERM/HUP trap is what makes /bin/sh
# (dash) run the EXIT trap on a signal; without it an interrupted run leaves the emulator up.
.PHONY: test-cosmos
test-cosmos: ensure-hatch
	@set -e; p="lg-cosmos-$$$$"; cert="$$(mktemp)"; \
	trap 'rm -f "$$cert"; docker compose -p "$$p" -f docker-compose.cosmos.yml down -v' EXIT; \
	trap 'exit 130' INT TERM HUP; \
	docker compose -p "$$p" -f docker-compose.cosmos.yml up -d --wait; \
	curl -fsk https://localhost:8081/_explorer/emulator.pem -o "$$cert"; \
	COSMOS_EMULATOR_ENDPOINT=https://localhost:8081 COSMOS_REQUIRED=true \
	REQUESTS_CA_BUNDLE="$$cert" SSL_CERT_FILE="$$cert" $(HATCH) run cosmos:test

.PHONY: test-table
test-table: ensure-hatch
	@set -e; p="lg-table-$$$$"; \
	trap 'docker compose -p "$$p" -f docker-compose.azurite.yml down -v' EXIT; \
	trap 'exit 130' INT TERM HUP; \
	docker compose -p "$$p" -f docker-compose.azurite.yml up -d --wait; \
	AZURITE_REQUIRED=true $(HATCH) run table:test

.PHONY: test-openapi
test-openapi: ensure-hatch
	@$(HATCH) run openapi:test

.PHONY: cov
cov: ensure-hatch
	@$(HATCH) run cov
	@echo "Open htmlcov/index.html in your browser to view the coverage report."
	@echo "coverage.xml generated for Codecov upload."

# e2e lanes: a run whose inputs are unset fails instead of skipping (tests/e2e/conftest.py), so a
# missing deployment can never look like a pass. One local host serves only the native routes.
.PHONY: e2e-local
e2e-local: ensure-hatch
	@echo "Running the native-route e2e tests against a local host (E2E_BASE_URL=http://localhost:7071)..."
	@E2E_BASE_URL=http://localhost:7071 $(HATCH) run e2e-local

.PHONY: e2e-azure
e2e-azure: ensure-hatch
	@echo "Running all e2e tests against Azure (E2E_BASE_URL, E2E_BASE_URL_B and E2E_STREAM_BASE_URL must be set)..."
	@$(HATCH) run e2e-azure

.PHONY: build
build: ensure-hatch
	@$(HATCH) build

.PHONY: publish-test
publish-test: ensure-hatch
	@$(HATCH) publish --repo test

.PHONY: version
version: ensure-hatch
	@echo "Current version:"
	@$(HATCH) version

.PHONY: docs
docs:
	@if [ -n "$$CI" ]; then \
		echo "CI detected: running MkDocs directly"; \
		python -m pip install --upgrade pip > /dev/null 2>&1 || true; \
		pip install "mkdocs<2.0" "mkdocs-material<10.0" "mkdocstrings[python]<1.0" > /dev/null 2>&1; \
		mkdocs build; \
	else \
		$(MAKE) ensure-hatch > /dev/null; \
		$(HATCH) run mkdocs build; \
	fi

.PHONY: docs-serve
docs-serve: ensure-hatch
	@$(HATCH) run docs

.PHONY: doctor
doctor:
	@echo "Python version:" && $(PYTHON) --version
	@echo "Installed packages:" && $(HATCH) env run pip list || echo "No hatch environment found"
	@echo "Azure Function Core Tools version:" && func --version || echo "func not found"
	@echo "Pre-commit hook installed:"
	@if [ -f .git/hooks/pre-commit ]; then echo yes; else echo no; fi

.PHONY: clean
clean:
	@rm -rf *.egg-info dist build __pycache__ .pytest_cache

.PHONY: clean-all
clean-all: clean
	@find . -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
	@rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage coverage.xml htmlcov .DS_Store $(VENV_DIR) site

.PHONY: help
help:
	@echo "Available commands:" && \
	grep -E '^\.PHONY: ' Makefile | cut -d ':' -f2 | xargs -n1 echo "  - make"
