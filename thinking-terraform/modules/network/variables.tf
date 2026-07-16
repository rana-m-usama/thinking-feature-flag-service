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
    Memorystore addresses from. /16 is Google's recommendation; /24 is plenty for two
    instances and leaves the rest of the space free. Cannot be resized in place — it is
    a delete-and-recreate, which means destroying the database.
  EOT
  type        = number
  default     = 24
}
