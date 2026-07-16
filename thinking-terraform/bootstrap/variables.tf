variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Region for the state bucket. Match the region live/ deploys into."
  type        = string
  default     = "asia-south1"
}

variable "billing_account_id" {
  description = <<-EOT
    Billing account ID (e.g. 01ABCD-234567-89EFGH), from `gcloud billing accounts list`.
    Leave empty to skip the budget alert — creating one needs billing.budgets.create on
    the billing account itself, which a project-level role does not grant.
  EOT
  type        = string
  default     = ""
}

variable "budget_amount_usd" {
  description = "Budget ceiling for alerts. Defaults to the free-tier credit grant."
  type        = number
  default     = 300
}
