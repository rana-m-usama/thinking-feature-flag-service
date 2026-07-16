output "service_name" {
  value = google_cloud_run_v2_service.main.name
}

output "service_uri" {
  description = "The *.run.app URL. Publicly reachable with Google-managed TLS unless enable_load_balancer restricts ingress."
  value       = google_cloud_run_v2_service.main.uri
}

output "service_id" {
  value = google_cloud_run_v2_service.main.id
}

output "migrate_job_name" {
  description = "CI executes this before shifting traffic."
  value       = google_cloud_run_v2_job.migrate.name
}

output "runtime_service_account" {
  value = google_service_account.run.email
}

output "migrate_service_account" {
  value = google_service_account.migrate.email
}
