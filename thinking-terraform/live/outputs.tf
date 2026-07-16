output "service_url" {
  description = "The deployed application URL — this is what goes in the README."
  value       = local.enable_load_balancer ? module.loadbalancer[0].url : module.service.service_uri
}

output "cloud_run_url" {
  description = "Direct run.app URL. Stops answering once a load balancer restricts ingress."
  value       = module.service.service_uri
}

output "load_balancer_ip" {
  description = "Point an A record here. The managed cert cannot provision until you do."
  value       = local.enable_load_balancer ? module.loadbalancer[0].ip_address : null
}

output "migrate_job_name" {
  description = "CI executes this before shifting any traffic."
  value       = module.service.migrate_job_name
}

output "artifact_registry_url" {
  value = module.github_oidc.artifact_registry_url
}

# --- GitHub Actions secrets ----------------------------------------------------------
# Set these two in the repo. Note what is NOT here: no service account key, because
# Workload Identity Federation does not use one.
output "wif_provider" {
  description = "GitHub secret WIF_PROVIDER"
  value       = module.github_oidc.workload_identity_provider
}

output "wif_service_account" {
  description = "GitHub secret WIF_SERVICE_ACCOUNT"
  value       = module.github_oidc.deployer_service_account
}

# --- Secrets -------------------------------------------------------------------------
output "admin_api_key_secret" {
  description = <<-EOT
    Secret Manager secret holding the bootstrap credential for POST /api/v1/tenants.
    Read the VALUE from Secret Manager, not from here:

      gcloud secrets versions access latest --secret=<this>

    Deliberately returns the secret's NAME rather than its value. `terraform output` is
    routinely piped into logs and CI transcripts; the name is safe there, the key is not.
  EOT
  value       = module.secrets.admin_api_key_secret_id
}

output "database_url_secret" {
  value = module.database.database_url_secret_id
}

output "database_private_ip" {
  description = "Reachable only from inside the VPC. There is no public path to it."
  value       = module.database.private_ip
}
