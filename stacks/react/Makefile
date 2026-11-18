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

# Ignored advisories (each one weakens this gate; keep the list short):
# - GHSA-fc5q-39xc-vjpf: picomatch <=4.0.7 ReDoS. No patched release exists
#   (4.0.7 is the latest), and it reaches this stack only through dev tooling
#   (vite, vitest via tinyglobby/fdir). Remove the ignore once a fixed
#   picomatch ships and `bun update picomatch` picks it up.
.PHONY: security
security: ## Dependency CVE audit
	@bun audit --ignore=GHSA-fc5q-39xc-vjpf

.PHONY: pr_check
# Coverage in the recipe, after the prerequisites: a red lint or security scan
# stops the check before the slow suite runs.
pr_check: lint security ## lint + security + coverage
	@$(MAKE) --no-print-directory coverage
