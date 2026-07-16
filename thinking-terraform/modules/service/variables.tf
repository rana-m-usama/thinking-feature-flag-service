variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }

variable "image" {
  description = <<-EOT
    Container image. Terraform sets this ONCE at creation and then ignores it forever
    (see lifecycle in main.tf) — CI owns the tag from then on. The default is Google's
    hello image purely to break the bootstrap cycle: Cloud Run cannot be created without
    an image, and Artifact Registry is empty until CI has run at least once.
  EOT
  type        = string
  default     = "gcr.io/cloudrun/hello"
}

variable "app_env" {
  description = "APP_ENV. Anything but 'local' switches the logger to Cloud Logging JSON."
  type        = string
  default     = "production"
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "network_name" { type = string }
variable "subnet_name" { type = string }

variable "redis_url" {
  description = "Plain env var, not a secret: a private IP with no AUTH, unreachable outside the VPC."
  type        = string
}

variable "database_url_secret_id" { type = string }
variable "admin_api_key_secret_id" { type = string }

variable "min_instances" {
  description = "0 scales to zero and costs nothing at rest, at the price of ~2s cold starts on someone's checkout page."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Ceiling on autoscaling. Also a cost ceiling — and a guard on Cloud SQL, whose connection limit is what actually breaks first."
  type        = number
  default     = 10
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
  description = <<-EOT
    Requests per container. 80 is Cloud Run's default and right for an async app whose
    hot path is a Redis read plus a SHA-256 — it is I/O-bound, not CPU-bound, so one
    container serves many concurrent requests happily. Lowering it multiplies instance
    count (and cost) for no throughput gain.
  EOT
  type        = number
  default     = 80
}

variable "cache_ttl_seconds" {
  description = "Backstop only. Writes invalidate immediately; this bounds staleness if an invalidation is ever lost."
  type        = number
  default     = 300
}

variable "rate_limit_per_minute" {
  type    = number
  default = 1000
}

variable "db_pool_size" {
  description = <<-EOT
    Pool size PER CONTAINER, which is the trap. Cloud SQL db-f1-micro allows ~25
    connections total. max_instances x (pool_size + max_overflow) must stay under that or
    autoscaling under load exhausts the connection limit and every instance starts
    failing — the failure mode looks like a database outage caused by traffic the service
    handled fine. 10 x (2+3) = 50 would be too many; 10 x (2+1) = 30 is still too many.
    See live/terraform.production.tfvars where these are actually set.
  EOT
  type        = number
  default     = 2
}

variable "db_max_overflow" {
  type    = number
  default = 1
}

variable "enable_load_balancer" {
  description = "When true, ingress is locked to the load balancer and the run.app URL stops answering."
  type        = bool
  default     = false
}
