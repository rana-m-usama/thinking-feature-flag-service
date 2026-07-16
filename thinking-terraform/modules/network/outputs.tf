output "network_id" {
  value = google_compute_network.main.id
}

output "network_name" {
  value = google_compute_network.main.name
}

output "subnet_id" {
  value = google_compute_subnetwork.main.id
}

output "subnet_name" {
  value = google_compute_subnetwork.main.name
}

output "psa_connection" {
  description = "Datastores must depend on this — a private IP cannot be assigned before the peering exists."
  value       = google_service_networking_connection.psa.id
}

output "psa_range_name" {
  description = "Memorystore reuses this range rather than allocating a second peering block."
  value       = google_compute_global_address.psa_range.name
}
