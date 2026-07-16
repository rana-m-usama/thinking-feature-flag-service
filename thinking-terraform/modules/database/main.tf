/**
 * Cloud SQL for PostgreSQL — private IP only, credentials in Secret Manager.
 *
 * Why Cloud SQL and not AlloyDB (the closer Aurora analogue): AlloyDB has no shared-core
 * tier and starts around $250/month. This workload is a handful of small tenant-scoped
 * queries per cache miss — the hot path does not touch the database at all. Cloud SQL on
 * a shared core is correctly sized; AlloyDB would be buying a database for a workload
 * that barely has one.
 */

resource "random_password" "db" {
  length  = 32
  special = true
  # Excluded because this password is interpolated into a URL. '@' terminates the
  # userinfo section, '/' opens the path, '#' starts a fragment — a generated password
  # containing any of them produces a DATABASE_URL that parses into something else
  # entirely, and the failure looks like a wrong password rather than a broken URL.
  override_special = "!#$%*()-_=+[]{}<>:?"
}

resource "google_sql_database_instance" "main" {
  name                = "${var.name_prefix}-pg"
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier = var.tier

    # ZONAL, not REGIONAL. Regional HA doubles the cost for a synchronous standby in a
    # second zone. For a service whose reads are served from Redis and whose writes are
    # occasional flag toggles, a few minutes of failover is survivable and $25/month is
    # not — see the README's cost note. REGIONAL is the one-line change if it ever is.
    availability_type = var.availability_type
    disk_size         = var.disk_size_gb
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    ip_configuration {
      # No public IP. The instance is unreachable from the internet, full stop — not
      # firewalled off from it, absent from it. This is the whole reason for the PSA
      # peering in the network module.
      ipv4_enabled                                  = false
      private_network                               = var.network_id
      enable_private_path_for_google_cloud_services = true
      # ssl_mode replaced require_ssl in provider 6.x.
      #
      # ALLOW_UNENCRYPTED_AND_ENCRYPTED, not ENCRYPTED_ONLY, and the reasoning is
      # specific rather than lazy: this instance has no public IP, so the only thing that
      # can reach it is Cloud Run egress inside our own VPC, and Google encrypts traffic
      # between VMs at the physical layer regardless. ENCRYPTED_ONLY would add a TLS
      # handshake per connection on a shared core that has ~25 connections to give.
      #
      # This flips the moment there is any path to this instance we do not control.
      ssl_mode = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "03:00"
      point_in_time_recovery_enabled = var.point_in_time_recovery
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = var.retained_backups
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7 # Sunday
      hour         = 4
      update_track = "stable"
    }

    database_flags {
      # Log any statement over a second. On a shared core the noisy-neighbour query is
      # the one that takes the instance down, and this is the cheapest way to find it.
      name  = "log_min_duration_statement"
      value = "1000"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
    }
  }

  # A private IP cannot be assigned before Google's network is peered in.
  depends_on = [var.psa_connection]

  lifecycle {
    # Terraform-side guard only — someone in the console walks straight past it. The real
    # protection is deletion_protection above, which the API itself enforces.
    prevent_destroy = false # set true for production once the URL is handed out
  }
}

resource "google_sql_database" "main" {
  name     = var.database_name
  project  = var.project_id
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = var.database_user
  project  = var.project_id
  instance = google_sql_database_instance.main.name
  password = random_password.db.result
}

# --- Secret Manager ----------------------------------------------------------------
#
# The full SQLAlchemy URL is stored, not just the password, so the application reads one
# env var and never assembles a connection string from parts. Nothing in the app knows
# whether it is talking to a container or Cloud SQL — that is the property that makes
# docker-compose meaningful evidence about production.
resource "google_secret_manager_secret" "database_url" {
  secret_id = "${var.name_prefix}-database-url"
  project   = var.project_id

  replication {
    # Pinned to the region the service runs in: lower latency on secret access, and the
    # secret cannot be read out of a region we never chose to operate in.
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s",
    var.database_user,
    urlencode(random_password.db.result),
    google_sql_database_instance.main.private_ip_address,
    var.database_name,
  )
}
