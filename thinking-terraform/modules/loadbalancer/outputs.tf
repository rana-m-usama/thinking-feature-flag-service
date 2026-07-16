output "ip_address" {
  description = "Point an A record here. The managed cert stays in PROVISIONING until you do."
  value       = google_compute_global_address.main.address
}

output "url" {
  value = "https://${var.domain}"
}

output "certificate_name" {
  value = google_compute_managed_ssl_certificate.main.name
}
