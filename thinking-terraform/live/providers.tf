provider "google" {
  project = var.project_id
  region  = var.region

  # Required, not optional, and the failure mode is deeply misleading.
  #
  # Some Google APIs bill the call against a "quota project". ADC user credentials
  # (gcloud auth application-default login) carry none, so those APIs reject the request
  # with "Error code 16: Request had invalid authentication credentials" — which reads as
  # a broken login and sends you re-authenticating for an hour. The credentials are fine;
  # the call simply has no project to bill against.
  #
  # servicenetworking is one such API, which means Private Service Access — and therefore
  # every private IP in this stack — cannot be created without these two lines.
  user_project_override = true
  billing_project       = var.project_id

  default_labels = {
    application = "flagsvc"
    environment = var.environment
    managed_by  = "terraform"
  }
}
