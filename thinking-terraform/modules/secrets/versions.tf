terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    # Declared rather than inherited from the root. A module that silently relies on its
    # caller's provider set breaks the moment it is reused somewhere that lacks it.
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
