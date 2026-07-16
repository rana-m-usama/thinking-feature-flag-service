variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }

variable "cloud_run_service_name" {
  type = string
}

variable "domain" {
  description = "A domain you control. Managed certs validate via DNS; there is no way around owning one."
  type        = string
}

variable "enable_cloud_armor" {
  type    = bool
  default = true
}

variable "armor_rate_limit_per_minute" {
  description = "Per-IP edge limit. Loose by design — many tenants can share one NAT IP, so this must never act as a tenant quota."
  type        = number
  default     = 6000
}
