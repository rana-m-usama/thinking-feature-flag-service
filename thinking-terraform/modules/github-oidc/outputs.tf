output "workload_identity_provider" {
  description = "Set as WIF_PROVIDER in GitHub. Full resource name for google-github-actions/auth."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  description = "Set as WIF_SERVICE_ACCOUNT in GitHub."
  value       = google_service_account.deployer.email
}

output "artifact_registry_url" {
  value = "${google_artifact_registry_repository.main.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.main.repository_id}"
}
