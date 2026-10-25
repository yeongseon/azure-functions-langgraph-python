VENV_DIR := .venv
PYTHON ?= python3
PIP := $(VENV_DIR)/bin/pip
HATCH ?= hatch
HOST_PYTHON ?= $(HATCH) run python

.PHONY: bootstrap
bootstrap:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Creating virtual environment..."; \
		$(PYTHON) -m venv $(VENV_DIR); \
	fi
	@echo "Ensuring Hatch is installed in virtual environment..."
	@$(PIP) install hatch > /dev/null
	@echo "Hatch installed at $(VENV_DIR)/bin/hatch"

.PHONY: ensure-hatch
ensure-hatch:
	@command -v "$(HATCH)" >/dev/null || { echo 'Hatch is required; run make bootstrap, then set HATCH=.venv/bin/hatch'; exit 1; }

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
security: ensure-hatch security-inventory
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
	report="$$(mktemp)"; \
	trap 'rm -f "$$cert" "$$report"; docker compose -p "$$p" -f docker-compose.cosmos.yml down -v' EXIT; \
	COSMOS_EMULATOR_ENDPOINT=https://localhost:8081 COSMOS_REQUIRED=true \
	REQUESTS_CA_BUNDLE="$$cert" SSL_CERT_FILE="$$cert" $(HATCH) run cosmos:python -m pytest \
		-v --strict-markers -m integration tests/integration/test_cosmos_integration.py \
		--no-cov --junitxml="$$report"; \
	$(HATCH) run cosmos:python tools/check_junit.py "$$report" --area 'Cosmos integration=test_cosmos_integration'

.PHONY: test-table
test-table: ensure-hatch
	@set -e; p="lg-table-$$$$"; \
	trap 'docker compose -p "$$p" -f docker-compose.azurite.yml down -v' EXIT; \
	trap 'exit 130' INT TERM HUP; \
	docker compose -p "$$p" -f docker-compose.azurite.yml up -d --wait; \
	report="$$(mktemp)"; \
	trap 'rm -f "$$report"; docker compose -p "$$p" -f docker-compose.azurite.yml down -v' EXIT; \
	AZURITE_REQUIRED=true $(HATCH) run table:python -m pytest -v --strict-markers -m integration \
		tests/integration/test_table_store_integration.py tests/integration/test_checkpoint_conformance.py \
		tests/test_production_persistent_agent_example.py --no-cov --junitxml="$$report"; \
	$(HATCH) run table:python tools/check_junit.py "$$report" \
		--area 'Table integration=table_store_integration' \
		--area 'Blob conformance=checkpoint_conformance' \
		--area 'production persistent agent=production_persistent_agent'

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
	@set -e; report="$$(mktemp)"; trap 'rm -f "$$report"' EXIT; \
	$(HATCH) run python -m pytest -v -m e2e tests/e2e/ --no-cov --tb=short --junitxml="$$report"; \
	$(HATCH) run python tools/check_junit.py "$$report" \
		--area 'native=test_langgraph_e2e' --area 'locks=test_langgraph_lock_e2e' \
		--area 'streaming=test_true_streaming_e2e'

.PHONY: host-smoke
host-smoke: ensure-hatch
	@$(HOST_PYTHON) tools/run_host_smoke.py

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
	@echo 'check/test require Hatch, Python 3.11+, ruff, mypy, pytest, pytest-cov and project dependencies.'
	@echo 'test-table/test-cosmos require Docker Compose and free loopback ports; test-cosmos also needs curl and the cosmos extra.'
	@echo 'host-smoke requires func, azurite and the dev extra; e2e-azure requires deployed E2E_BASE_URL, E2E_BASE_URL_B and E2E_STREAM_BASE_URL.'
	@command -v "$(HATCH)" >/dev/null || { echo 'Missing Hatch: run make bootstrap'; exit 1; }
	@env_path="$$($(HATCH) env find)"; \
	test -x "$$env_path/bin/python" || { echo 'Missing Hatch environment: run make install'; exit 1; }; \
	"$$env_path/bin/python" -c 'import sys, ruff, mypy, pytest, pytest_cov, azure_functions_langgraph; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required")'

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

SECURITY_PYTHON ?= $(HATCH) run python
SECURITY_OUTPUT ?= security-results
PROFILE ?= base
SCAN_OUTCOME ?= unknown
.PHONY: security-inventory security-bandit-evidence security-semgrep-evidence security-audit-policy security-runtime-audit security-runtime-report
security-inventory security-bandit-evidence:
	@$(SECURITY_PYTHON) -m tools.security_evidence bandit --output "$(SECURITY_OUTPUT)/bandit"
security-semgrep-evidence:
	@$(SECURITY_PYTHON) -m tools.security_evidence semgrep --outcome "$(SCAN_OUTCOME)" --output "$(SECURITY_OUTPUT)/semgrep"; code=$$?; test $$code -eq 0 -o $$code -eq 3
security-audit-policy:
	@$(SECURITY_PYTHON) scripts/check_audit_exceptions.py .github/pip-audit-ignore.txt
security-runtime-audit:
	@test -n "$(WHEEL)" && test -f "$(WHEEL)" || { echo "WHEEL must name one existing candidate wheel"; exit 2; }
	@$(SECURITY_PYTHON) -m tools.audit_candidate --wheel "$(WHEEL)" --profile "$(PROFILE)" --output "$(SECURITY_OUTPUT)/runtime-audit"
security-runtime-report:
	@$(SECURITY_PYTHON) -m tools.runtime_audit "$(REPORT)" --exit-status "$(AUDIT_EXIT)" --output "$(SECURITY_OUTPUT)/runtime-audit"
