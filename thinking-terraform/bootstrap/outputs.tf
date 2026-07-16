output "state_bucket" {
  description = "Feed this to live/backend.*.hcl as `bucket`."
  value       = google_storage_bucket.state.name
}

output "project_number" {
  description = "Needed for the Workload Identity Federation principal string."
  value       = data.google_project.current.number
}

output "enabled_apis" {
  value = sort([for s in google_project_service.required : s.service])
}
