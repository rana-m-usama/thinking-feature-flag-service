variable "project_id" { type = string }
variable "region" { type = string }
variable "name_prefix" { type = string }

variable "network_id" {
  type = string
}

variable "psa_range_name" {
  description = "Reuses the network module's PSA range rather than allocating a second block."
  type        = string
}

variable "psa_connection" {
  description = "Sequencing dependency — the peering must exist first."
  type        = string
}

variable "tier" {
  description = "BASIC (single node) or STANDARD_HA (replica, ~2x cost). BASIC is correct for a rebuildable cache."
  type        = string
  default     = "BASIC"
}

variable "memory_size_gb" {
  description = "1 is the minimum SKU. The compiled flag set is kilobytes per tenant; this is floor pricing, not sizing."
  type        = number
  default     = 1
}
