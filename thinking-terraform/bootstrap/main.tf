/**
 * Bootstrap — run once, by hand, before anything in live/.
 *
 * Solves the chicken-and-egg: live/ keeps its state in a GCS bucket, but that bucket
 * cannot be created by the configuration that stores its state in it. So this root uses
 * LOCAL state and creates the two things everything else assumes already exist: the
 * state bucket and the enabled APIs.
 *
 *   cd bootstrap && terraform init && terraform apply
 *
 * This is the only Terraform here that CI never runs.
 */

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # Local state on purpose. See the header.
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Required for APIs that bill against a quota project — billingbudgets is one.
  #
  # ADC user credentials (from `gcloud auth application-default login`) carry no quota
  # project, so those APIs reject the call with "requires a quota project, which is not
  # set by default". These two lines attach the x-goog-user-project header so the API has
  # a project to bill the call against. A service account would not need this; a human's
  # ADC does.
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "current" {
  project_id = var.project_id
}

# Every API the project needs, enabled once here rather than scattered across modules.
# The provider does not enable APIs implicitly, and a missing one surfaces as an opaque
# 403 several minutes into an apply — usually on whichever resource takes longest.
resource "google_project_service" "required" {
  for_each = toset([
    "compute.googleapis.com",              # VPC, load balancer
    "run.googleapis.com",                  # Cloud Run
    "sqladmin.googleapis.com",             # Cloud SQL
    "redis.googleapis.com",                # Memorystore
    "secretmanager.googleapis.com",        # Secret Manager
    "artifactregistry.googleapis.com",     # container images
    "servicenetworking.googleapis.com",    # Private Service Access -> Cloud SQL private IP
    "vpcaccess.googleapis.com",            # Cloud Run VPC egress
    "monitoring.googleapis.com",           # dashboards, alerts, uptime checks
    "logging.googleapis.com",              # log-based metrics
    "cloudresourcemanager.googleapis.com", # IAM bindings
    "iam.googleapis.com",                  # service accounts
    "iamcredentials.googleapis.com",       # Workload Identity Federation
    "sts.googleapis.com",                  # WIF token exchange
    "billingbudgets.googleapis.com",       # budget alerts
  ])

  project = var.project_id
  service = each.value

  # Leave APIs enabled on destroy. Disabling one is project-wide and breaks anything else
  # using it; re-enabling takes minutes. Not something a routine destroy should trigger.
  disable_on_destroy = false
}

# --- Terraform state ---------------------------------------------------------------
#
# This bucket will hold the generated database password and admin API key in PLAINTEXT.
# `random_password` writes its result to state; that is unavoidable. Everything below
# exists because of that: the state bucket is exactly as sensitive as Secret Manager,
# and treating it as "just a bucket" is how the Secret Manager story becomes theatre.
resource "google_storage_bucket" "state" {
  name     = "${var.project_id}-tfstate"
  project  = var.project_id
  location = var.region

  # A truncated state file with no history is an afternoon of importing resources by hand.
  versioning {
    enabled = true
  }

  # Access governed by IAM alone. Without this a stray object ACL can make state readable
  # and nothing in the IAM policy would show it.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      num_newer_versions = 20
    }
    action {
      type = "Delete"
    }
  }

  # Terraform can be rewritten. Knowing which live resources it already owns cannot.
  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.required]
}

# --- Budget alert ------------------------------------------------------------------
#
# $300 of credit and no billing alarm is how a take-home turns into a surprise invoice.
# This does NOT cap spend — GCP has no hard stop — it emails at the thresholds below.
# A smoke detector, not a sprinkler.
resource "google_billing_budget" "credit_guard" {
  count = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "flagsvc-credit-guard"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_amount_usd)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # Forecast-based: fires when the run rate implies overspend, which lands days before
  # actual spend crosses the line — the only one of these alerts with time to act on.
  threshold_rules {
    threshold_percent = 0.9
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.required]
}
