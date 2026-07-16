output "admin_api_key_secret_id" {
  value = google_secret_manager_secret.admin_api_key.secret_id
}

output "admin_api_key" {
  description = "The bootstrap credential for POST /api/v1/tenants. Read it from Secret Manager rather than state."
  value       = google_secret_manager_secret_version.admin_api_key.secret_data
  sensitive   = true
}
