REPO_ROOT := $(shell git rev-parse --show-toplevel)
include $(REPO_ROOT)/mk/shared.mk

.PHONY: deps
deps: ## Install dependencies with bun
	@bun install

.PHONY: format
format: ## Auto-format with Biome
	@bunx biome format --write .

.PHONY: lint
lint: ## Biome check (lint+format) + tsc type-check
	@bunx biome check .
	@bunx tsc --noEmit

.PHONY: tests
tests: ## Run Vitest
	@bunx vitest run

.PHONY: coverage
coverage: ## Vitest with istanbul coverage + 80% gate; writes test-results.xml (JUnit)
	@bunx vitest run --coverage --reporter=default --reporter=junit --outputFile.junit=test-results.xml

.PHONY: test_speed_check
# Reads the last `make coverage` run's JUnit. Re-runs each suspect alone, timed
# from its own JUnit ({junit}). The budget lives in .github/pr-reviewer.json ->
# test_speed.
test_speed_check: ## Per-test speed guard over the last coverage run
	@python3 $(REPO_ROOT)/scripts/check_test_speed.py --junit test-results.xml --allowlist slow_test_allowlist.txt --config $(REPO_ROOT)/.github/pr-reviewer.json --rerun 'bunx vitest run {test} --reporter=junit --outputFile.junit={junit}'

.PHONY: security
security: ## Dependency CVE audit
	@bun audit

.PHONY: pr_check
# Coverage, THEN the speed check, in the recipe: the check reads the JUnit file
# coverage writes, an order `make -j` would not guarantee between prerequisites.
pr_check: lint security ## lint + security + coverage + per-test speed check
	@$(MAKE) --no-print-directory coverage && $(MAKE) --no-print-directory test_speed_check
