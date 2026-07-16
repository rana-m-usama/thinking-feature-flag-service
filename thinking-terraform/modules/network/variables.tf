variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  description = "Prefix for every resource, e.g. flagsvc-production."
  type        = string
}

variable "subnet_cidr" {
  description = "Range for the regional subnet. Only Cloud Run egress consumes addresses here."
  type        = string
  default     = "10.10.0.0/24"
}

variable "psa_prefix_length" {
  description = <<-EOT
    Prefix length for the Private Service Access range Google allocates Cloud SQL and
    Memorystore addresses from.

    /16, which is Google's recommendation, and NOT a round-up-for-safety choice — /24
    provably does not work. Each service provider carves an entire SUBNET out of this
    range, not individual addresses: Memorystore takes a /29, but Cloud SQL demands a
    full /24 for itself. Inside a /24 total, Memorystore's /29 leaves ~248 free
    addresses and still no room for Cloud SQL, which fails with the thoroughly
    misleading "Couldn't find free blocks in allocated IP ranges".

    Reasoning about address counts here is the trap. Reason about subnet blocks.

    Cannot be resized in place — changing it replaces the address, which means every
    instance allocated from it must be destroyed first.
  EOT
  type        = number
  default     = 16
}
