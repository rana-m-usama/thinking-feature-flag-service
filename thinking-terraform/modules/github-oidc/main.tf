/**
 * Workload Identity Federation — the GCP counterpart to your github-oidc module.
 *
 * Same trust model you already run on AWS: GitHub mints a short-lived OIDC token, GCP
 * exchanges it for an access token, no long-lived credential ever exists. A service
 * account JSON key in repo secrets is the single most commonly leaked GCP credential —
 * it does not expire, it works from anywhere, and it is one `echo $KEY` in a debug step
 * away from a public build log.
 */

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "${var.name_prefix}-gh-pool"
  project                   = var.project_id
  display_name              = "GitHub Actions"
  description               = "Keyless CI auth for ${var.github_repository}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"
  project                            = var.project_id
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  # THE security boundary of this entire module.
  #
  # Without a condition, this pool trusts EVERY repository on GitHub — anyone could push
  # a workflow in their own repo and mint tokens for the deployer SA below. Google now
  # refuses to create an unconditioned provider precisely because of how often this went
  # wrong. It is the exact hazard as a too-broad `sub` in an AWS OIDC trust policy, just
  # with a louder failure.
  #
  # Pinned to the full repository, not just the owner: `repository_owner == 'x'` still
  # trusts every repo in the org, including one a compromised contractor account creates.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# --- Deployer service account ------------------------------------------------------
resource "google_service_account" "deployer" {
  account_id   = "${var.name_prefix}-deployer"
  project      = var.project_id
  display_name = "GitHub Actions deployer for ${var.name_prefix}"
}

# Only workflows from the pinned repository may impersonate the deployer. This binding
# plus the attribute_condition above are what stand between a public GitHub and your
# project — belt and braces on purpose, because either alone is one typo from open.
resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Least privilege for CI. Notably NOT roles/editor, which is what most tutorials reach
# for and which would let a compromised workflow delete the database.
resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/run.developer",           # deploy revisions, split traffic, execute jobs
    "roles/artifactregistry.writer", # push images
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# Deploying a Cloud Run service that RUNS AS another service account requires actAs on
# that account. Scoped to the two runtime accounts specifically — the project-level
# roles/iam.serviceAccountUser would grant actAs on every account in the project,
# including the deployer itself, which is a privilege-escalation path.
resource "google_service_account_iam_member" "deployer_act_as" {
  for_each           = toset(var.runtime_service_accounts)
  service_account_id = "projects/${var.project_id}/serviceAccounts/${each.value}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# --- Artifact Registry -------------------------------------------------------------
resource "google_artifact_registry_repository" "main" {
  repository_id = var.name_prefix
  project       = var.project_id
  location      = var.region
  format        = "DOCKER"
  description   = "Container images for ${var.name_prefix}"

  # Images accumulate forever otherwise, one per merge, at ~265MB each. Keep the recent
  # ones — a rollback target that has been garbage-collected is not a rollback target.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }
}
