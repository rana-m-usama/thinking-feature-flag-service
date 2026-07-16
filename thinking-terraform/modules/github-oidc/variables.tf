variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }

variable "github_repository" {
  description = <<-EOT
    Full owner/repo, e.g. "acme/feature-flag-service". This is the trust boundary — it is
    interpolated straight into the provider's attribute_condition. Owner-only would trust
    every repository in the org.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repository))
    error_message = "Must be owner/repo — a bare owner would widen the trust boundary to the whole org."
  }
}

variable "runtime_service_accounts" {
  description = "Service account emails CI may deploy as. Scoped actAs, not project-wide serviceAccountUser."
  type        = list(string)
  default     = []
}
