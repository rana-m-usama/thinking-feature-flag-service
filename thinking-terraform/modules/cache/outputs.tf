output "host" {
  value = google_redis_instance.main.host
}

output "port" {
  value = google_redis_instance.main.port
}

output "redis_url" {
  description = "Passed to Cloud Run as REDIS_URL. Not a secret — it is a private IP, unreachable outside the VPC, and no AUTH is configured."
  value       = "redis://${google_redis_instance.main.host}:${google_redis_instance.main.port}/0"
}
