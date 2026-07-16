# Terraform wrapper — the safety rail for the live/ pattern.
#
# live/ is one root module configured by backend.<env>.hcl + terraform.<env>.tfvars. That
# is DRY, and it has one sharp edge: nothing in Terraform stops you initialising with
# staging's backend and applying with production's tfvars. You would plan production
# resources into staging's state, and the first sign of trouble would be a plan proposing
# to destroy everything.
#
# Every target here derives BOTH filenames from one ENV variable, so they cannot disagree.
# That is the entire point. Do not run terraform in live/ by hand.
#
#   make init ENV=production
#   make plan ENV=production
#   make apply ENV=production

.PHONY: help bootstrap init plan apply destroy fmt validate lint output url admin-key check-env

ENV ?=
LIVE := live
BOOTSTRAP := bootstrap

VALID_ENVS := staging production

help:
	@echo "Usage: make <target> ENV=<staging|production>"
	@echo ""
	@echo "  bootstrap    Create state bucket + enable APIs (run once, local state)"
	@echo "  init         terraform init with the matching backend config"
	@echo "  plan         Plan against the matching tfvars"
	@echo "  apply        Apply"
	@echo "  destroy      Destroy in dependency-safe order (see the target)"
	@echo "  output       Show outputs"
	@echo "  url          Print the deployed service URL"
	@echo "  admin-key    Read the bootstrap API key out of Secret Manager"
	@echo "  fmt          terraform fmt -recursive"
	@echo "  validate     terraform validate (no credentials needed)"
	@echo "  lint         fmt -check + validate — what CI runs"

check-env:
ifndef ENV
	$(error ENV is required. Use: make $(MAKECMDGOALS) ENV=production)
endif
	@echo "$(VALID_ENVS)" | grep -qw "$(ENV)" || (echo "ERROR: ENV must be one of: $(VALID_ENVS)"; exit 1)
	@test -f $(LIVE)/backend.$(ENV).hcl || (echo "ERROR: missing $(LIVE)/backend.$(ENV).hcl"; exit 1)
	@test -f $(LIVE)/terraform.$(ENV).tfvars || (echo "ERROR: missing $(LIVE)/terraform.$(ENV).tfvars"; exit 1)

# Run once, by hand, before anything else. Local state — it creates the bucket the rest
# of the repo stores state in, so it cannot itself use that bucket.
bootstrap:
	cd $(BOOTSTRAP) && terraform init && terraform apply

# -reconfigure is not optional when switching environments. Without it Terraform tries to
# MIGRATE state from the previously-initialised backend into the new one — which would
# copy staging's state into production's bucket and ask you to confirm in a prompt that
# looks routine.
init: check-env
	cd $(LIVE) && terraform init -backend-config=backend.$(ENV).hcl -reconfigure

plan: check-env
	cd $(LIVE) && terraform plan -var-file=terraform.$(ENV).tfvars -out=$(ENV).tfplan

# Applies the saved plan rather than re-planning. Re-planning at apply time means you
# approve one set of changes and apply whatever the world looks like seconds later.
apply: check-env
	cd $(LIVE) && terraform apply $(ENV).tfplan || \
		(echo ""; echo "No saved plan? Run: make plan ENV=$(ENV)"; exit 1)

# Destroy order is not cosmetic. The PSA peering cannot be torn down while Cloud SQL or
# Memorystore still hold IPs allocated from it — Terraform gets the ordering wrong often
# enough that the delete hangs ~20 minutes and then fails, leaving a half-destroyed
# network to unpick by hand. Take the datastores out first, explicitly.
#
# Also: production sets db_deletion_protection = true, so this will refuse until that is
# flipped and applied. That refusal is the feature.
destroy: check-env
	@echo "Destroying $(ENV). Datastores first, then the network."
	cd $(LIVE) && terraform destroy -var-file=terraform.$(ENV).tfvars \
		-target=module.database -target=module.cache -auto-approve
	cd $(LIVE) && terraform destroy -var-file=terraform.$(ENV).tfvars

output: check-env
	cd $(LIVE) && terraform output

url: check-env
	@cd $(LIVE) && terraform output -raw service_url

# The key itself is never a Terraform output — `terraform output` lands in CI logs. This
# reads it from Secret Manager, which is where it actually lives.
admin-key: check-env
	@cd $(LIVE) && gcloud secrets versions access latest \
		--secret=$$(terraform output -raw admin_api_key_secret) \
		--project=$$(terraform output -raw -json 2>/dev/null | true; grep '^project_id' terraform.$(ENV).tfvars | cut -d'"' -f2)

fmt:
	terraform fmt -recursive .

# No credentials needed — validate checks syntax and types, not the cloud.
validate:
	cd $(BOOTSTRAP) && terraform init -backend=false >/dev/null && terraform validate
	cd $(LIVE) && terraform init -backend=false >/dev/null && terraform validate

lint:
	terraform fmt -recursive -check -diff .
	$(MAKE) validate
