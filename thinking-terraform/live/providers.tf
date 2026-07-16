provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    application = "flagsvc"
    environment = var.environment
    managed_by  = "terraform"
  }
}
