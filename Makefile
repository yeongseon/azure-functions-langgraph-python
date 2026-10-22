VENV_DIR := .venv
PYTHON ?= python
HATCH ?= hatch
ifeq ($(CI),)
RUN := $(HATCH) run python
else
RUN := $(PYTHON)
endif

.PHONY: bootstrap ensure-hatch install install-ci shell reset hatch-clean
bootstrap ensure-hatch:
	@command -v $(HATCH) >/dev/null || { echo "Install Hatch once in an isolated tool environment"; exit 1; }
install: ensure-hatch
	@$(HATCH) env create
	@if [ -z "$$CI" ]; then $(MAKE) precommit-install; fi
install-ci:
	@$(PYTHON) -m pip install -e ".[dev,docs,azure-blob,azure-table,cosmos,openapi]"
shell: ensure-hatch
	@$(HATCH) shell
reset: clean-all install
hatch-clean: ensure-hatch
	@$(HATCH) env remove

.PHONY: format format-check style typecheck lint security check lint-workflows check-all
format:
	@$(RUN) -m ruff format src tests tools/verification*.py
format-check:
	@$(RUN) -m ruff format --check src tests tools/verification*.py
style: format-check
	@$(RUN) -m ruff check src tests tools/verification*.py
typecheck:
	@$(RUN) -m mypy src tests tools/verification*.py
lint: style typecheck
security:
	@$(RUN) -m bandit -r src
lint-workflows:
	@$(RUN) tools/lint_release_workflows.py
	@$(RUN) tools/lint_workflow_pins.py
	@$(RUN) tools/lint_hatch_matrix.py
check: style typecheck security lint-workflows
check-all: verify

.PHONY: test integration verify report doctor clean deterministic
test integration verify report clean deterministic:
	@$(RUN) -m tools.verification $@
doctor:
	@if [ -n "$$CI" ]; then $(PYTHON) -m tools.verification doctor; \
	else environment=$$($(HATCH) env find) && test -x "$$environment/bin/python" && \
	PATH="$$environment/bin:$$PATH" "$$environment/bin/python" -m tools.verification doctor; fi

.PHONY: test-table test-cosmos test-openapi host-smoke wheel-smoke cov e2e-azure e2e-local
test-table:
	@$(RUN) -m tools.verification table --owned
test-cosmos:
	@$(RUN) -m tools.verification cosmos --owned
test-openapi:
	@$(HATCH) run openapi:python -m tools.verification focused openapi -m pytest tests/test_openapi_real_companion_interop.py --no-cov
host-smoke:
	@$(RUN) -m tools.verification_host
wheel-smoke:
	@$(RUN) -m tools.verification_wheel
cov:
	@$(RUN) -m tools.verification test --cov-report=html
e2e-azure:
	@$(RUN) -m tools.verification azure-e2e
e2e-local:
	@$(MAKE) host-smoke

.PHONY: certify-examples-published certify-examples-candidate build docs docs-serve
certify-examples-published:
	@$(RUN) -m tools.verification focused examples-published scripts/certify_primary_examples.py published --output-dir test-results/examples-published/resolver
certify-examples-candidate:
	@test -n "$(WHEEL)" || { echo "WHEEL=/path/to/candidate.whl is required"; exit 2; }
	@$(RUN) -m tools.verification focused examples-candidate scripts/certify_primary_examples.py candidate --wheel "$(WHEEL)" --output-dir test-results/examples-candidate/resolver
build:
	@$(RUN) -m tools.verification focused build -m build
docs:
	@$(RUN) -m tools.verification focused docs -m mkdocs build --strict
docs-serve:
	@$(RUN) -m mkdocs serve

.PHONY: precommit precommit-install publish-test version clean-all help
precommit precommit-install: ensure-hatch
	@$(HATCH) run $@
publish-test: ensure-hatch
	@$(HATCH) publish --repo test
version: ensure-hatch
	@$(HATCH) version
clean-all: clean
	@find . -type d -name "__pycache__" -not -path "./.git/*" -exec rm -rf {} +
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -not -path "./.git/*" -delete
	@rm -rf .DS_Store $(VENV_DIR)
help:
	@echo "Canonical: install install-ci doctor format format-check check test integration verify e2e-azure report clean"
	@echo "check-all: exact alias of verify; focused commands produce PARTIAL evidence"
	@echo "Focused: test-table test-cosmos test-openapi deterministic host-smoke wheel-smoke certify-examples-published certify-examples-candidate"
