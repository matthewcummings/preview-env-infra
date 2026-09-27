# Thin wrappers: every target calls one script or CLI, so the logic stays in Python (D33)
# and works the same on macOS, Linux and Windows via WSL2. `make` (or `make help`) lists them.

.DEFAULT_GOAL := help
.PHONY: help test lint synth doctor bootstrap deploy-baseline setup-github deploy-main \
	preview plan teardown smoke allow-ip disallow-ip list-ips

RECONCILE := uv run python -m reconciler

# Optional arguments, only passed when set.
NO_AWS_FLAG := $(if $(NO_AWS),--no-aws)
CIDR_FLAG := $(if $(CIDR),--cidr $(CIDR))
SPEC_FLAG := $(if $(SPEC),--spec $(SPEC))
PLAN_TARGET := $(if $(BRANCH),--branch $(BRANCH),$(if $(GROUP),--group $(GROUP),--env main))

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-17s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test: ## Run all tests (no AWS, no network)
	uv run pytest

lint: ## Ruff lint + format check
	uv run ruff check
	uv run ruff format --check

synth: ## cdk synth, no AWS credentials needed
	npx cdk synth -q

doctor: ## Check tools, AWS credentials, region, CDK bootstrap, GitHub owner
	uv run python scripts/doctor.py

bootstrap: ## One-time CDK bootstrap of the account/region
	npx cdk bootstrap

deploy-baseline: ## Deploy preview-baseline (reuses an existing GitHub OIDC provider); first time: add ALLOW_MY_IP=1
	uv run python scripts/deploy_baseline.py $(if $(ALLOW_MY_IP),--allow-my-ip)

setup-github: ## Set GitHub repo variables + dispatch secret from preview-baseline outputs (DRY_RUN=1 to preview)
	uv run python scripts/setup_github.py $(if $(DRY_RUN),--dry-run)

deploy-main: ## Reconcile the shared main env (newest built main image per service)
	$(RECONCILE) apply --env main

preview: ## Reconcile the env for BRANCH=preview/<group>[/...] (create, update or tear down)
	$(RECONCILE) apply --branch $(BRANCH)

plan: ## Show the plan for BRANCH=... or GROUP=... (default: main); NO_AWS=1 for SHAs only
	$(RECONCILE) plan $(PLAN_TARGET) $(NO_AWS_FLAG)

teardown: ## Delete the env for GROUP=... now, even if its branches still exist
	$(RECONCILE) teardown --group $(GROUP)

smoke: ## Smoke-test ENV=... (SPEC=envspec.json also checks the deployed SHAs)
	uv run python scripts/smoke_test.py $(ENV) $(SPEC_FLAG)

allow-ip: ## Allow your public IP (or CIDR=...) through every env's ALB
	uv run python scripts/allow_ip.py add $(CIDR_FLAG)

disallow-ip: ## Remove your public IP (or CIDR=...) from the ALB allowlist
	uv run python scripts/allow_ip.py remove $(CIDR_FLAG)

list-ips: ## Show the ALB allowlist
	uv run python scripts/allow_ip.py list
