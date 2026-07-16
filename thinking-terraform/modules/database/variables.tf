variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }

variable "network_id" {
  description = "VPC to attach the private IP to."
  type        = string
}

variable "psa_connection" {
  description = "Private Service Access connection. Sequencing dependency — a private IP cannot exist before it."
  type        = string
}

variable "tier" {
  description = <<-EOT
    Machine tier. db-f1-micro is shared-core (~$8/mo) and correct for this workload: the
    hot path is served from Redis and never reaches Postgres. db-g1-small (~$25/mo) if
    write volume grows. Custom tiers (db-custom-N-M) start well above both.
  EOT
  type        = string
  default     = "db-f1-micro"
}

variable "edition" {
  description = <<-EOT
    ENTERPRISE or ENTERPRISE_PLUS. Must be explicit — the API default varies by Postgres
    version and ENTERPRISE_PLUS rejects every shared-core tier, forcing db-perf-optimized-*
    at ~$300+/month. ENTERPRISE is the only edition where db-f1-micro exists.
  EOT
  type        = string
  default     = "ENTERPRISE"

  validation {
    condition     = contains(["ENTERPRISE", "ENTERPRISE_PLUS"], var.edition)
    error_message = "Must be ENTERPRISE or ENTERPRISE_PLUS."
  }
}

variable "availability_type" {
  description = "ZONAL or REGIONAL. REGIONAL doubles cost for a synchronous standby."
  type        = string
  default     = "ZONAL"
}

variable "disk_size_gb" {
  type    = number
  default = 10
}

variable "database_name" {
  type    = string
  default = "flagsvc"
}

variable "database_user" {
  type    = string
  default = "flagsvc"
}

variable "deletion_protection" {
  description = "API-enforced, unlike Terraform's prevent_destroy. Leave true for production."
  type        = bool
  default     = true
}

variable "point_in_time_recovery" {
  description = "PITR needs WAL archiving; adds storage cost. Worth it in production, not in staging."
  type        = bool
  default     = false
}

variable "retained_backups" {
  type    = number
  default = 7
}
