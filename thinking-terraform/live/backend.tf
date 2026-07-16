/**
 * Partial backend configuration.
 *
 * Deliberately empty — bucket and prefix come from backend.<env>.hcl:
 *
 *   terraform init -backend-config=backend.production.hcl -reconfigure
 *
 * Hardcoding a bucket here would tie this root to one environment and defeat the whole
 * live/ pattern. `-reconfigure` matters when switching environments: without it Terraform
 * tries to MIGRATE state from the old backend to the new one, which would copy staging's
 * state into production's bucket and is not a good afternoon.
 */
terraform {
  backend "gcs" {}
}
