terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source = "hashicorp/google"
      # Pinned to a major. Cloud Run v2 resources and direct VPC egress landed in 5.x/6.x;
      # ~> 6.0 takes patches and minors but never a major, since a major has broken
      # Cloud Run resource shapes before.
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
