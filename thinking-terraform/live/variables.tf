# --- Identity ------------------------------------------------------------------------
variable "project_id" {
  type = string
}

variable "environment" {
  description = "Drives name_prefix and APP_ENV. Must match the tfvars/backend file in use."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Must be staging or production."
  }
}

variable "region" {
  description = <<-EOT
    Every regional resource lands here. Cloud Run, Cloud SQL and Memorystore MUST share a
    region — cross-region private IP traffic is either impossible or billed as inter-region
    egress on every single query.
  EOT
  type        = string
  default     = "asia-south1"
}

# --- Network -------------------------------------------------------------------------
variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/24"
}

# --- Database ------------------------------------------------------------------------
variable "db_tier" {
  type    = string
  default = "db-f1-micro"
}

variable "db_availability_type" {
  type    = string
  default = "ZONAL"
}

variable "db_disk_size_gb" {
  type    = number
  default = 10
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "db_point_in_time_recovery" {
  type    = bool
  default = false
}

# --- Cache ---------------------------------------------------------------------------
variable "redis_tier" {
  type    = string
  default = "BASIC"
}

variable "redis_memory_gb" {
  type    = number
  default = 1
}

# --- Service -------------------------------------------------------------------------
variable "image" {
  description = "Only used on first create; CI owns it after that. See modules/service lifecycle."
  type        = string
  default     = "gcr.io/cloudrun/hello"
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "min_instances" {
  type    = number
  default = 0
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "cpu" {
  type    = string
  default = "1"
}

variable "memory" {
  type    = string
  default = "512Mi"
}

variable "concurrency" {
  type    = number
  default = 80
}

variable "cache_ttl_seconds" {
  type    = number
  default = 300
}

variable "rate_limit_per_minute" {
  type    = number
  default = 1000
}

variable "db_pool_size" {
  type    = number
  default = 2
}

variable "db_max_overflow" {
  type    = number
  default = 1
}

# --- Load balancer -------------------------------------------------------------------
variable "domain" {
  description = <<-EOT
    A domain you control. Empty means NO load balancer at all — see the note in main.tf.
    Setting this switches Cloud Run ingress to load-balancer-only, so the run.app URL
    stops answering and DNS must point at the LB IP before anything works.
  EOT
  type        = string
  default     = ""
}

variable "enable_cloud_armor" {
  type    = bool
  default = true
}

variable "armor_rate_limit_per_minute" {
  type    = number
  default = 6000
}

# --- CI/CD ---------------------------------------------------------------------------
variable "github_repository" {
  description = "owner/repo. The Workload Identity trust boundary."
  type        = string
}

# --- Monitoring ----------------------------------------------------------------------
variable "alert_email" {
  type = string
}

variable "latency_threshold_ms" {
  type    = number
  default = 500
}
