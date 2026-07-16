/**
 * Secrets that are not owned by another module.
 *
 * The database URL lives in modules/database because it is only knowable once the
 * instance has a private IP. This holds the rest.
 */

resource "random_password" "admin_api_key" {
  length = 48
  # URL-safe alphabet only. This value is pasted into curl commands, CI env vars and HTTP
  # headers; a '#' or '&' in it turns a working request into a baffling one.
  special = false
}

resource "google_secret_manager_secret" "admin_api_key" {
  secret_id = "${var.name_prefix}-admin-api-key"
  project   = var.project_id

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "admin_api_key" {
  secret      = google_secret_manager_secret.admin_api_key.id
  secret_data = "adm_${random_password.admin_api_key.result}"
}
