# Shared make targets, included by each stack Makefile via the git repo root so
# they resolve whether the including Makefile is in stacks/<stack>/ or at root.

.PHONY: help
help: ## Print available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

.PHONY: cc
cc: ## Run Claude Code with useful config settings
	@caffeinate -di claude --enable-auto-mode --remote-control

.PHONY: apply_repo_settings_bootstrap
apply_repo_settings_bootstrap: ## Apply setup-safe settings; defer new CI requirements until the setup PR merges
	@test -n "$(TARGET_REPO)" || { echo "TARGET_REPO=owner/repository is required"; exit 2; }
	@python3 $(REPO_ROOT)/scripts/apply_repo_settings.py --repo "$(TARGET_REPO)" --phase bootstrap

.PHONY: apply_repo_settings
apply_repo_settings: ## Reconcile the final main ruleset + PR/security settings after CI exists on main
	@test -n "$(TARGET_REPO)" || { echo "TARGET_REPO=owner/repository is required"; exit 2; }
	@python3 $(REPO_ROOT)/scripts/apply_repo_settings.py --repo "$(TARGET_REPO)" --phase final

.PHONY: finalize_repo_settings
finalize_repo_settings: apply_repo_settings ## Explicit post-merge alias for the final repository-settings pass
