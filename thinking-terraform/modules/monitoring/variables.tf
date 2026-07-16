variable "project_id" { type = string }
variable "name_prefix" { type = string }

variable "service_name" {
  description = "Cloud Run service name — scopes every log filter."
  type        = string
}

variable "alert_email" {
  description = "Where alerts go. GCP sends a confirmation mail that must be clicked before the channel delivers anything."
  type        = string
}

variable "uptime_check_host" {
  description = "Hostname to probe: the custom domain if there is an LB, otherwise the run.app host with no scheme."
  type        = string
}

variable "latency_threshold_ms" {
  description = <<-EOT
    p95 alert threshold. 500ms is ~1000x a cache-hit evaluation (sub-millisecond) and
    ~20x a cache miss. Deliberately loose: an alert that fires on normal cold starts gets
    muted within a week, and a muted alert is worse than none.
  EOT
  type        = number
  default     = 500
}
