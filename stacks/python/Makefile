REPO_ROOT := $(shell git rev-parse --show-toplevel)
include $(REPO_ROOT)/mk/shared.mk

.PHONY: deps
deps: ## Install runtime + dev dependencies
	@uv sync --dev

.PHONY: format
format: ## Auto-format with black
	@uv run black src/ tests/

.PHONY: lint
lint: ## black --check + ruff + mypy
	@uv run black --check src/ tests/
	@uv run ruff check src/ tests/
	@uv run mypy

.PHONY: tests
tests: ## Run pytest (excludes e2e/integration)
	@uv run pytest -v --tb=short

.PHONY: coverage
coverage: ## pytest with coverage + 80% gate; writes test-results.xml (JUnit)
	@uv run pytest --cov=src --cov-report=term-missing --cov-report=xml --cov-fail-under=80 --junitxml=test-results.xml

.PHONY: test_speed_check
# Reads the last `make coverage` run's JUnit. Re-runs each suspect alone, serially
# (-p no:xdist, addopts cleared) and timed from its own JUnit ({junit}). The budget
# lives in .github/pr-reviewer.json -> test_speed.
test_speed_check: ## Per-test speed guard over the last coverage run
	@python3 $(REPO_ROOT)/scripts/check_test_speed.py --junit test-results.xml --allowlist tests/slow_test_allowlist.txt --config $(REPO_ROOT)/.github/pr-reviewer.json --rerun 'uv run pytest -p no:xdist -o addopts="" {test} --junitxml={junit}'

.PHONY: security
security: ## bandit SAST scan
	@uv run bandit -r src/

.PHONY: pr_check
# Coverage, THEN the speed check, in the recipe: the check reads the JUnit file
# coverage writes, an order `make -j` would not guarantee between prerequisites.
pr_check: lint security ## lint + security + coverage + per-test speed check
	@$(MAKE) --no-print-directory coverage && $(MAKE) --no-print-directory test_speed_check
